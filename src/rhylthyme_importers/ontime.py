"""
Ontime importer - converts Ontime rundowns (https://www.getontime.no,
https://github.com/cpvalente/ontime) to Rhylthyme run-of-show programs.

Ontime keeps a show as a rundown: an ordered list of events, each with a
clock start, an end and a duration, optionally linked to the event before
it, gathered into groups, with milestones (markers) and delays (runtime
adjustments) in between. It reads and writes that rundown in two shapes,
and this importer takes both:

- the project file (``.json``, "Download project" / the server's db.json):
  version 4 keeps ``rundowns`` keyed by id; version 3 kept one ``rundown``
  list with ``block`` entries for sections;
- the rundown as a table: Ontime's ``.xlsx`` export and import template,
  a Google Sheet, or the same table saved or pasted as CSV/TSV or Markdown.
  The column names are Ontime's (``time start``, ``time end``,
  ``duration``, ``cue``, ``title``, ``note``, ``link start``, ``skip``,
  ``timer type``, ...), matched without regard to case, with a few common
  spellings (``start``, ``end``, ``name``, ``notes``) accepted as well.
  Any other column is kept on each step as a custom field. Cells are read
  as Ontime reads them: ``0930`` and ``9:30`` are clock times, a bare
  ``15`` is minutes, ``1h30`` is an hour and a half, and any text in a
  yes/no column but "false" is yes.

Each group becomes a track and each event a fixed-length step. A linked
event, or one that starts exactly when the event before it ends, starts
after that step; any other event keeps its clock time as an offset from
the start of the show, so gaps and overlaps survive. Days roll over as in
Ontime: an event whose clock time is not after the previous event's, or
that follows an event running past midnight, is on the next day. A
milestone (or an event with no length) is a marker, not a span: it becomes
an alert on the step after it, or at the end of the last step. Skipped
events are left out, and delays are ignored, as Ontime's own spreadsheet
export does: they adjust a running show, not the plan. Two deliberate
differences from Ontime's spreadsheet import: without a ``link start``
column, only rows with no start time follow on (Ontime links every row and
so drops the gaps the table shows), and a row needs a time column to be
read at all. Everything Ontime knows that a program has no field for (cue,
clock times, end action, timer type, colour, custom fields) is kept in
each step's ``metadata.ontime``.
"""

import csv
import io
import json
import re
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from xml.etree import ElementTree

from .base import BaseImporter, ImporterRegistry, ImportResult

DAY = 24 * 60 * 60

# Ontime's spreadsheet column names (its default import map), lowercased,
# and the field each one fills. The extra spellings are what hand-made
# run-of-show tables tend to use.
_COLUMNS = {
    'time start': 'timeStart', 'start': 'timeStart', 'start time': 'timeStart',
    'time end': 'timeEnd', 'end': 'timeEnd', 'end time': 'timeEnd',
    'duration': 'duration', 'length': 'duration',
    'cue': 'cue',
    'title': 'title', 'name': 'title',
    'note': 'note', 'notes': 'note',
    'link start': 'linkStart',
    'skip': 'skip',
    'flag': 'flag',
    'count to end': 'countToEnd',
    'colour': 'colour', 'color': 'colour',
    'end action': 'endAction',
    'timer type': 'timerType',
    'warning time': 'timeWarning',
    'danger time': 'timeDanger',
    'id': 'id',
}
_TIME_FIELDS = ('timeStart', 'timeEnd', 'duration', 'timeWarning', 'timeDanger')
_DURATION_FIELDS = ('duration', 'timeWarning', 'timeDanger')
_BOOL_FIELDS = ('linkStart', 'skip', 'flag', 'countToEnd')

# A row's ``timer type`` cell can instead say what kind of row it is.
_ROW_KINDS = {
    'group': 'group', 'group-start': 'group', 'block': 'group',
    'group-end': 'group-end',
    'milestone': 'milestone',
    'skip-import': 'skip-import',
}
_TIMER_TYPES = ('count-down', 'count-up', 'clock', 'none')

_SPREADSHEET_NS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
_REL_NS = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
_PKG_REL_NS = '{http://schemas.openxmlformats.org/package/2006/relationships}'

_GOOGLE_SHEET_RE = re.compile(r'^https?://docs\.google\.com/spreadsheets/d/([A-Za-z0-9_-]+)')


class OntimeFormatError(ValueError):
    """The input is not an Ontime project or rundown table."""


def google_sheet_csv_url(url: str) -> str:
    """The CSV export URL of a Google Sheet's share or edit link (keeping
    the tab in ``gid``); any other URL is returned unchanged. The sheet has
    to be shared as "anyone with the link can view" for the export to work
    without signing in."""
    m = _GOOGLE_SHEET_RE.match(url or '')
    if not m or '/export' in url:
        return url
    gid = re.search(r'[#?&]gid=(\d+)', url)
    return (f'https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv'
            + (f'&gid={gid.group(1)}' if gid else ''))


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------

class _SheetTime(float):
    """A spreadsheet number shown as a date or time: days since 1900, with
    the time of day as the fraction."""


def parse_time(value: Any, duration: bool = False) -> Optional[int]:
    """Seconds from a time of day or a duration, read the way Ontime reads
    what is typed into a cell.

    ``"10:30:00"``, ``"10:30"`` (hours and minutes), ``"1030"``/``"930"``
    (the same without a colon), ``"7:30 pm"``/``"7p"``, ``"1h30m"``,
    ``"1h30"`` (the trailing number is the next unit), ``"45m"``,
    ``"90s"``, an ISO date-time (its time of day), a bare one- or two-digit
    number (minutes), or a spreadsheet date/time cell (its time of day, or
    with ``duration`` the whole span). Empty or unreadable values are None.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, _SheetTime):
        return int(round(value * DAY)) if duration else int(round((value % 1) * DAY))
    if isinstance(value, (int, float)):
        # A number from a sheet whose format is unknown: a fraction is a
        # spreadsheet time, a whole number is minutes.
        if isinstance(value, float) and not value.is_integer():
            return int(round(value * DAY)) if duration or value < 1 else int(round((value % 1) * DAY))
        return int(value) * 60
    text = str(value).strip().lower()
    if not text or text in ('-', '--', 'n/a'):
        return None
    iso = re.match(r'^\d{4}-\d{2}-\d{2}[t ](\d{1,2}):(\d{2})(?::(\d{2}))?', text)
    if iso:
        h, m, s = iso.groups()
        return int(h) * 3600 + int(m) * 60 + int(s or 0)

    meridiem = re.search(r'(?<=[\d\s])([ap])\.?(?:m\.?)?$', text)
    am, pm = bool(meridiem and meridiem.group(1) == 'a'), bool(meridiem and meridiem.group(1) == 'p')
    if meridiem:
        text = text[:meridiem.start()].strip()

    if re.search(r'\d\s*[hms]', text):
        hours = re.search(r'(\d+)\s*h', text)
        minutes = re.search(r'(\d+)\s*m', text)
        seconds = re.search(r'(\d+)\s*s', text)
        h = int(hours.group(1)) if hours else 0
        m = int(minutes.group(1)) if minutes else 0
        s = int(seconds.group(1)) if seconds else 0
        # "1h30" and "1m30": a number after the last unit is the next one.
        tail = re.search(r'([hm])[a-z]*\s*(\d+)\s*$', text)
        if tail and tail.group(1) == 'h' and not minutes:
            m = int(tail.group(2))
        elif tail and tail.group(1) == 'm' and not seconds:
            s = int(tail.group(2))
        return h * 3600 + m * 60 + s

    parts = re.split(r'[:.,\s]+', text)
    if not all(p.isdigit() for p in parts) or len(parts) > 3:
        return None
    if len(parts) == 1:
        digits = parts[0]
        if len(digits) <= 2:
            if not (am or pm):
                return int(digits) * 60
            hours, minutes, secs = int(digits), 0, 0
        elif len(digits) == 3:
            hours, minutes, secs = int(digits[0]), int(digits[1:]), 0
        else:
            hours, minutes, secs = int(digits[:2]), int(digits[2:4]), int(digits[4:6] or 0)
    else:
        hours, minutes = int(parts[0]), int(parts[1])
        secs = int(parts[2]) if len(parts) == 3 else 0
    if hours == 12 and (am or pm):
        hours = 0
    if pm:
        hours += 12
    return hours * 3600 + minutes * 60 + secs


def _parse_bool(value: Any) -> Optional[bool]:
    """Ontime's rule for a yes/no cell: empty is unset, any text but
    "false" is true (it reads every cell as its text, so a 0 is true too)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if not text:
        return None
    return text != 'false'


def _clock(seconds: Optional[int]) -> Optional[str]:
    if seconds is None:
        return None
    seconds %= DAY
    return f'{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}'


def _millis_to_seconds(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(round(value / 1000))


# ---------------------------------------------------------------------------
# Reading: everything becomes a rundown (title + ordered entries)
# ---------------------------------------------------------------------------
#
# An entry is a dict with ``kind`` (event, group, group-end, milestone,
# delay) and the event fields in seconds; ``custom`` maps a column label to
# its text.

def _entries_from_project(data: Dict[str, Any], rundown_id: Optional[str]) -> Tuple[str, List[Dict], Dict]:
    """A version 4 project (``rundowns``), a version 3 one (``rundown``),
    or one bare version 4 rundown (``entries`` + ``order``)."""
    labels = {key: (field or {}).get('label') or key
              for key, field in (data.get('customFields') or {}).items()
              if isinstance(field, dict)}
    project = data.get('project') if isinstance(data.get('project'), dict) else {}
    info: Dict[str, Any] = {'projectTitle': project.get('title') or ''}
    version = (data.get('settings') or {}).get('version') if isinstance(data.get('settings'), dict) else None
    if version and version != '-':
        info['ontimeVersion'] = version

    if isinstance(data.get('rundowns'), dict) and data['rundowns']:
        rundowns = data['rundowns']
        if rundown_id:
            if rundown_id not in rundowns:
                matches = [k for k, r in rundowns.items() if (r or {}).get('title') == rundown_id]
                if not matches:
                    raise OntimeFormatError(
                        f"No rundown '{rundown_id}' in this project; it has: "
                        + ', '.join(f"{k} ({(r or {}).get('title', '')})" for k, r in rundowns.items()))
                rundown_id = matches[0]
            chosen = rundown_id
        else:
            # The first rundown that has events in it.
            chosen = next((k for k, r in rundowns.items()
                           if any(e['kind'] == 'event' for e in _v4_entries(r, labels))),
                          next(iter(rundowns)))
        others = [k for k in rundowns if k != chosen]
        if others:
            info['otherRundowns'] = [{'id': k, 'title': (rundowns[k] or {}).get('title', '')} for k in others]
        rundown = rundowns[chosen]
        info['rundownId'] = chosen
        return (rundown or {}).get('title') or '', _v4_entries(rundown, labels), info

    if isinstance(data.get('entries'), dict) and isinstance(data.get('order'), list):
        info['rundownId'] = data.get('id') or ''
        return data.get('title') or '', _v4_entries(data, labels), info

    if isinstance(data.get('rundown'), list):
        return '', _v3_entries(data['rundown'], labels), info

    raise OntimeFormatError(
        'This JSON is not an Ontime project: it has no "rundowns" (Ontime 4) '
        'or "rundown" (Ontime 3).')


def _event_fields(raw: Dict[str, Any], labels: Dict[str, str]) -> Dict[str, Any]:
    entry = {
        'id': raw.get('id'),
        'cue': raw.get('cue') or '',
        'title': raw.get('title') or '',
        'note': raw.get('note') or '',
        'colour': raw.get('colour') or '',
        'custom': {labels.get(k, k): v for k, v in (raw.get('custom') or {}).items() if v not in (None, '')},
    }
    for field in _TIME_FIELDS:
        entry[field] = _millis_to_seconds(raw.get(field))
    for field in _BOOL_FIELDS:
        if field in raw:
            entry[field] = bool(raw.get(field))
    for field in ('endAction', 'timerType', 'timeStrategy'):
        if raw.get(field):
            entry[field] = raw[field]
    # Ontime 3 had count-to-end as a timer type.
    if entry.get('timerType') == 'count-to-end':
        entry['timerType'], entry['countToEnd'] = 'count-down', True
    return entry


def _v4_entry(raw: Dict[str, Any], labels: Dict[str, str]) -> Optional[Dict[str, Any]]:
    kind = raw.get('type')
    if kind in ('event', 'milestone'):
        return dict(_event_fields(raw, labels), kind=kind)
    if kind == 'delay':
        return {'kind': 'delay', 'duration': _millis_to_seconds(raw.get('duration'))}
    return None


def _v4_entries(rundown: Dict[str, Any], labels: Dict[str, str]) -> List[Dict]:
    """In show order, as Ontime loads a rundown: ``order`` lists the top
    level, and a group (a ``block`` before 4.0) lists its own ``entries``.
    ``flatOrder`` and each entry's ``parent`` are derived from those, so
    they are only read when ``order`` is missing."""
    if not isinstance(rundown, dict):
        return []
    entries = rundown.get('entries') or {}
    out: List[Dict] = []
    order = rundown.get('order')
    if not isinstance(order, list) or not order:
        # No top-level order: the flat list, grouped by each entry's parent.
        current = None
        for entry_id in rundown.get('flatOrder') or []:
            raw = entries.get(entry_id)
            if not isinstance(raw, dict):
                continue
            if raw.get('type') in ('group', 'block'):
                if current:
                    out.append({'kind': 'group-end'})
                out.append(_group_entry(entry_id, raw))
                current = entry_id
                continue
            if current and raw.get('parent') != current:
                out.append({'kind': 'group-end'})
                current = None
            entry = _v4_entry(raw, labels)
            if entry:
                out.append(entry)
        return out
    for entry_id in order:
        raw = entries.get(entry_id)
        if not isinstance(raw, dict):
            continue
        if raw.get('type') in ('group', 'block'):
            out.append(_group_entry(entry_id, raw))
            for child_id in raw.get('entries') or []:
                child = entries.get(child_id)
                entry = _v4_entry(child, labels) if isinstance(child, dict) else None
                if entry:
                    out.append(entry)
            out.append({'kind': 'group-end'})
            continue
        entry = _v4_entry(raw, labels)
        if entry:
            out.append(entry)
    return out


def _group_entry(entry_id: str, raw: Dict[str, Any]) -> Dict[str, Any]:
    return {'kind': 'group', 'id': entry_id, 'title': raw.get('title') or '',
            'note': raw.get('note') or '', 'colour': raw.get('colour') or ''}


def _v3_entries(rundown: List[Any], labels: Dict[str, str]) -> List[Dict]:
    out: List[Dict] = []
    for raw in rundown:
        if not isinstance(raw, dict):
            continue
        kind = raw.get('type')
        if kind == 'block':
            # A version 3 block is a heading: everything after it, up to
            # the next block, is in its section.
            out.append(_group_entry(raw.get('id'), raw))
        elif kind == 'event':
            out.append(dict(_event_fields(raw, labels), kind='event'))
        elif kind == 'delay':
            out.append({'kind': 'delay', 'duration': _millis_to_seconds(raw.get('duration'))})
    return out


def _header_map(row: List[Any]) -> Optional[Dict[int, str]]:
    """Column index -> field for a header row, or None if this row is not
    one: it needs a time column and one more of Ontime's columns."""
    fields: Dict[int, str] = {}
    for i, cell in enumerate(row):
        name = re.sub(r'\s+', ' ', str(cell if cell is not None else '')).strip().lower()
        if not name:
            continue
        field = _COLUMNS.get(name)
        if field and field not in fields.values():
            fields[i] = field
        elif not field:
            fields[i] = 'custom:' + re.sub(r'\s+', ' ', str(cell)).strip()
    known = {f for f in fields.values() if not f.startswith('custom:')}
    if known & {'timeStart', 'timeEnd', 'duration'} and len(known) >= 2:
        return fields
    return None


def _find_header(rows: List[List[Any]]) -> Optional[Tuple[int, Dict[int, str]]]:
    for index, row in enumerate(rows):
        columns = _header_map(row)
        if columns:
            return index, columns
    return None


def _entries_from_rows(rows: List[List[Any]]) -> List[Dict]:
    """A rundown table: the header row is the first row naming a time
    column and another of Ontime's columns (Ontime's export starts with it;
    a template may have a title block above it)."""
    found = _find_header(rows)
    if not found:
        raise OntimeFormatError(
            "No header row found. An Ontime rundown table needs a 'time start', 'time end' or "
            "'duration' column and another of Ontime's columns, such as 'title' or 'cue'.")
    index, columns = found
    has_link_column = 'linkStart' in columns.values()

    out: List[Dict] = []
    for row in rows[index + 1:]:
        cells = {field: row[i] for i, field in columns.items() if i < len(row)}
        if not any(str(v).strip() for v in cells.values() if v is not None):
            continue
        timer = str(cells.get('timerType') or '').strip().lower()
        kind = _ROW_KINDS.get(timer, 'event')
        if kind == 'skip-import':
            continue
        if kind == 'event' and timer and timer not in _TIMER_TYPES and timer != 'event':
            continue  # Ontime does not import a row with an unknown type either
        entry: Dict[str, Any] = {'kind': kind, 'custom': {}}
        for field, value in cells.items():
            if field.startswith('custom:'):
                if value not in (None, '') and str(value).strip():
                    label = field[len('custom:'):]
                    entry['custom'][label] = value if isinstance(value, (int, float)) else str(value).strip()
            elif field in _TIME_FIELDS:
                entry[field] = parse_time(value, duration=field in _DURATION_FIELDS)
            elif field in _BOOL_FIELDS:
                flag = _parse_bool(value)
                if flag is not None:
                    entry[field] = flag
            elif field == 'timerType':
                if kind == 'event' and timer in _TIMER_TYPES:
                    entry['timerType'] = timer
            else:
                entry[field] = '' if value is None else str(value).strip()
        if kind == 'group-end':
            out.append({'kind': 'group-end'})
            continue
        if kind == 'group':
            out.append({'kind': 'group', 'id': entry.get('id'), 'title': entry.get('title', ''),
                        'note': entry.get('note', ''), 'colour': entry.get('colour', '')})
            continue
        if kind == 'event' and not has_link_column:
            # Without a link-start column there is no saying which rows
            # follow on; a row without a start time does.
            entry['linkStart'] = entry.get('timeStart') is None
        out.append(entry)
    return out


_MD_RULE = re.compile(r'^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$')


def _md_cells(line: str) -> List[str]:
    """A Markdown table row's cells; ``\\|`` is a pipe inside a cell."""
    text = line.strip()
    if text.startswith('|'):
        text = text[1:]
    if text.endswith('|') and not text.endswith('\\|'):
        text = text[:-1]
    return [c.strip().replace('\\|', '|') for c in re.split(r'(?<!\\)\|', text)]


def _rows_from_text(text: str) -> List[List[str]]:
    """A pasted or downloaded table: Markdown (anywhere in the text, with
    or without outer pipes), else tab-, comma- or semicolon-separated, the
    separator being the one that yields a rundown header."""
    lines = text.splitlines()
    for j in range(1, len(lines)):
        if '|' in lines[j] and _MD_RULE.match(lines[j]) and '|' in lines[j - 1]:
            rows = [_md_cells(lines[j - 1])]
            for line in lines[j + 1:]:
                if '|' not in line:
                    break
                rows.append(_md_cells(line))
            return rows
    candidates = [[row for row in csv.reader(io.StringIO(text), delimiter=d)] for d in ('\t', ',', ';')]
    for rows in candidates:
        if _find_header(rows[:30]):
            return rows
    return candidates[1]


# Built-in number formats that show a date or a time.
_DATE_FORMAT_IDS = set(range(14, 23)) | {45, 46, 47}


def _xlsx_number_formats(book: zipfile.ZipFile, names: set) -> Optional[List[str]]:
    """For each cell style (the ``s`` attribute), whether it shows a date
    or time ('date'), an elapsed time like [h]:mm ('elapsed') or a plain
    number ('number'). None when the workbook has no styles."""
    if 'xl/styles.xml' not in names:
        return None
    root = ElementTree.fromstring(book.read('xl/styles.xml'))
    codes = {int(f.get('numFmtId', -1)): f.get('formatCode', '')
             for f in root.iter(_SPREADSHEET_NS + 'numFmt')}
    kinds: List[str] = []
    xfs = root.find(_SPREADSHEET_NS + 'cellXfs')
    for xf in (xfs if xfs is not None else []):
        fmt = int(xf.get('numFmtId', 0))
        code = codes.get(fmt)
        if code is None:
            kinds.append('date' if fmt in _DATE_FORMAT_IDS else 'number')
            continue
        bare = re.sub(r'"[^"]*"|\\.|\[(?![hms]+\])[^\]]*\]', '', code).lower()
        if re.search(r'\[[hms]+\]', bare):
            kinds.append('elapsed')
        elif re.search(r'[hmsdy]', bare):
            kinds.append('date')
        else:
            kinds.append('number')
    return kinds


def _xlsx_sheets(blob: bytes) -> List[Tuple[str, List[List[Any]]]]:
    """(name, rows) for every worksheet of an .xlsx, in workbook order.
    Cells are text, numbers or booleans; formulas give their saved value."""
    try:
        book = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as e:
        raise OntimeFormatError(f'Not an .xlsx file: {e}')

    def xml(name: str):
        return ElementTree.fromstring(book.read(name))

    names = set(book.namelist())
    if 'xl/workbook.xml' not in names:
        raise OntimeFormatError('Not an .xlsx workbook.')
    shared: List[str] = []
    if 'xl/sharedStrings.xml' in names:
        for si in xml('xl/sharedStrings.xml').iter(_SPREADSHEET_NS + 'si'):
            shared.append(''.join(t.text or '' for t in si.iter(_SPREADSHEET_NS + 't')))
    formats = _xlsx_number_formats(book, names)
    targets = {}
    if 'xl/_rels/workbook.xml.rels' in names:
        for rel in xml('xl/_rels/workbook.xml.rels').iter(_PKG_REL_NS + 'Relationship'):
            target = rel.get('Target', '').lstrip('/')
            targets[rel.get('Id')] = target if target.startswith('xl/') else 'xl/' + target
    sheets = []
    for sheet in xml('xl/workbook.xml').iter(_SPREADSHEET_NS + 'sheet'):
        path = targets.get(sheet.get(_REL_NS + 'id'))
        if not path or path not in names:
            continue
        rows: List[List[Any]] = []
        for row in xml(path).iter(_SPREADSHEET_NS + 'row'):
            cells: List[Any] = []
            for c in row.iter(_SPREADSHEET_NS + 'c'):
                col = _column_index(c.get('r', ''))
                if col is not None:
                    cells.extend([None] * (col - len(cells)))
                cells.append(_cell_value(c, shared, formats))
            rows.append(cells)
        sheets.append((sheet.get('name', ''), rows))
    return sheets


def _column_index(ref: str) -> Optional[int]:
    letters = re.match(r'[A-Z]+', ref or '')
    if not letters:
        return None
    n = 0
    for ch in letters.group(0):
        n = n * 26 + ord(ch) - 64
    return n - 1


def _cell_value(c, shared: List[str], formats: Optional[List[str]]) -> Any:
    kind = c.get('t')
    if kind == 'inlineStr':
        return ''.join(t.text or '' for t in c.iter(_SPREADSHEET_NS + 't'))
    v = c.find(_SPREADSHEET_NS + 'v')
    if v is None or v.text is None:
        return None
    if kind == 's':
        return shared[int(v.text)]
    if kind == 'b':
        return v.text == '1'
    if kind in ('str', 'e'):
        return v.text
    try:
        number = float(v.text)
    except ValueError:
        return v.text
    if formats is None:
        return int(number) if number.is_integer() else number
    style = int(c.get('s', 0))
    shown = formats[style] if style < len(formats) else 'number'
    if shown in ('date', 'elapsed'):
        return _SheetTime(number)
    # A plain number is read as the text it shows, as Ontime does.
    return str(int(number)) if number.is_integer() else repr(number)


# ---------------------------------------------------------------------------
# Writing the program
# ---------------------------------------------------------------------------

def _slug(text: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', (text or '').lower()).strip('-')[:40].rstrip('-')


def _event_length(entry: Dict[str, Any], start_clock: Optional[int]) -> int:
    """Seconds an event lasts once it starts at ``start_clock``. Ontime
    keeps either the end (lock-end) or the duration (lock-duration); a row
    with a duration keeps the duration, one with only an end keeps the end,
    and a linked event's start moves to the previous end either way."""
    if entry.get('kind') == 'milestone':
        return 0
    duration, end = entry.get('duration'), entry.get('timeEnd')
    strategy = entry.get('timeStrategy') or ('lock-duration' if duration else 'lock-end')
    if strategy == 'lock-end' and end is not None and start_clock is not None:
        return (end - start_clock) % DAY
    if duration:
        return int(duration)
    start = entry.get('timeStart')
    if start is not None and end is not None:
        return (end - start) % DAY
    return 0


class _ProgramBuilder:
    """Lays the rundown out in show time (seconds from the first event)."""

    def __init__(self):
        self.tracks: List[Dict[str, Any]] = []
        self.track_ends: Dict[str, int] = {}
        self.step_ids: set = set()
        self.skipped: List[str] = []
        self.delays = 0
        self.length = 0
        # Clock seconds (possibly negative, or past a day) of show time 0,
        # once an event with a clock time has said.
        self.origin: Optional[int] = None
        self.prev: Optional[Dict[str, Any]] = None  # stepId, start, end

    def unique_id(self, *candidates: str) -> str:
        base = next((s for s in (_slug(c) for c in candidates) if s), 'event')
        step_id, n = base, 2
        while step_id in self.step_ids:
            step_id, n = f'{base}-{n}', n + 1
        self.step_ids.add(step_id)
        return step_id

    def open_track(self, title: str, note: str = '', group_id: Optional[str] = None) -> Dict[str, Any]:
        name = title or f'Part {len(self.tracks) + 1}'
        track_id = _slug(name) or 'part'
        existing = {t['trackId'] for t in self.tracks}
        n = 2
        while track_id in existing:
            track_id, n = f'{_slug(name) or "part"}-{n}', n + 1
        track: Dict[str, Any] = {'trackId': track_id, 'name': name, 'steps': []}
        if note:
            track['description'] = note
        if group_id:
            track['metadata'] = {'ontime': {'groupId': group_id}}
        self.tracks.append(track)
        self.track_ends[track_id] = 0
        return track

    def start_of(self, entry: Dict[str, Any]) -> Tuple[int, Optional[int]]:
        """(show time the event starts, the show's origin with it)."""
        start_clock = entry.get('timeStart')
        prev = self.prev
        if prev is None:
            return 0, (start_clock if start_clock is not None else None)
        if entry.get('linkStart') or start_clock is None:
            return prev['end'], self.origin
        origin = self.origin if self.origin is not None else start_clock - prev['end']
        # Ontime's day rule: the next day when the previous event (if it
        # has a length) runs to or past midnight, or when this one starts
        # at or before the previous one's clock time.
        prev_abs = origin + prev['start']
        prev_clock, day = prev_abs % DAY, prev_abs // DAY
        prev_length = prev['end'] - prev['start']
        if prev_length > 0 and (prev_clock + prev_length >= DAY or start_clock <= prev_clock):
            day += 1
        return day * DAY + start_clock - origin, origin

    def place(self, entry: Dict[str, Any], track: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """The event as a step, or None when it has no length (a marker)."""
        start, origin = self.start_of(entry)
        clock_start = None if origin is None else (origin + start) % DAY
        duration = _event_length(entry, clock_start if clock_start is not None else entry.get('timeStart'))
        if duration <= 0:
            return None
        self.origin = origin

        # Steps in a track never overlap: an event that starts before the
        # last one in its track ends goes to an overlap track of its own.
        if start < self.track_ends[track['trackId']]:
            track = self.overlap_track(track, start)

        if self.prev is None or start == 0:
            trigger: Dict[str, Any] = {'type': 'programStart'}
        elif start == self.prev['end']:
            trigger = {'type': 'afterStep', 'stepId': self.prev['stepId']}
        else:
            trigger = {'type': 'programStartOffset', 'offsetSeconds': start}

        title = entry.get('title') or ''
        cue = entry.get('cue') or ''
        step_id = self.unique_id(cue and f'{cue} {title}' or title, cue, entry.get('id') or '')
        step: Dict[str, Any] = {
            'stepId': step_id,
            'name': title or (f'Cue {cue}' if cue else 'Event'),
            'startTrigger': trigger,
            'duration': {'type': 'fixed', 'seconds': int(duration)},
        }
        if entry.get('note'):
            step['description'] = entry['note']
        ontime: Dict[str, Any] = {}
        if entry.get('id'):
            ontime['id'] = entry['id']
        if cue:
            ontime['cue'] = cue
        if clock_start is not None:
            ontime['timeStart'] = _clock(clock_start)
            ontime['timeEnd'] = _clock(clock_start + int(duration))
        for field in ('endAction', 'timerType', 'countToEnd', 'flag', 'colour', 'timeStrategy'):
            if entry.get(field) not in (None, '', False):
                ontime[field] = entry[field]
        for field in ('timeWarning', 'timeDanger'):
            if entry.get(field):
                ontime[field + 'Seconds'] = entry[field]
        if entry.get('custom'):
            ontime['custom'] = entry['custom']
        step['metadata'] = {'ontime': ontime}

        track['steps'].append(step)
        end = start + int(duration)
        self.track_ends[track['trackId']] = end
        self.length = max(self.length, end)
        self.prev = {'stepId': step_id, 'start': start, 'end': end, 'step': step}
        return step

    def overlap_track(self, track: Dict[str, Any], start: int) -> Dict[str, Any]:
        base = track.get('_base', track)
        for t in self.tracks:
            if t.get('_base', t) is base and self.track_ends[t['trackId']] <= start:
                return t
        extra = self.open_track(f"{base['name']} (overlap)")
        extra['_base'] = base
        return extra


def _attach_markers(step: Dict[str, Any], markers: List[Dict[str, Any]], event: str) -> None:
    """Milestones (and events with no length) as alerts on a step, and
    as they were in Ontime in its metadata."""
    alerts = step.setdefault('alerts', [])
    kept = step['metadata']['ontime'].setdefault('markers', [])
    for m in markers:
        text = ' '.join(x for x in (m.get('cue') or '', m.get('title') or '') if x).strip() or 'Milestone'
        alerts.append({'event': event, 'message': text[:200]})
        fields = ('kind', 'id', 'cue', 'title', 'note', 'custom')
        kept.append({k: m.get(k) for k in fields if m.get(k)})


def build_program(title: str, entries: List[Dict], info: Dict[str, Any], importer: BaseImporter,
                  source_url: str) -> Dict[str, Any]:
    b = _ProgramBuilder()
    markers: List[Dict[str, Any]] = []
    marker_count = 0
    has_groups = any(e['kind'] == 'group' for e in entries)
    default_track: Optional[Dict[str, Any]] = None
    track: Optional[Dict[str, Any]] = None
    for entry in entries:
        kind = entry['kind']
        if kind == 'group':
            track = b.open_track(entry.get('title') or '', entry.get('note') or '', entry.get('id'))
            continue
        if kind == 'group-end':
            track = None
            continue
        if kind == 'delay':
            b.delays += 1
            continue
        if kind == 'milestone':
            # A marker, not a span (and never skipped: milestones have no
            # skip in Ontime): an alert when the next event starts.
            markers.append(entry)
            continue
        if entry.get('skip'):
            b.skipped.append(entry.get('title') or entry.get('cue') or '(untitled)')
            continue
        if track is None:
            if default_track is None:
                # Beside groups, the events outside them are the rundown's
                # own; without groups the one track is the whole show.
                default_track = b.open_track('Rundown' if has_groups else (title or 'Rundown'))
            track = default_track
        step = b.place(entry, track)
        if step is None:
            markers.append(entry)
            continue
        if markers:
            _attach_markers(step, markers, 'start')
            marker_count += len(markers)
            markers = []

    for t in b.tracks:
        t.pop('_base', None)
    tracks = [t for t in b.tracks if t['steps']]
    if not tracks:
        raise OntimeFormatError('The rundown has no events with a length to import.')
    if markers:
        # Markers after the last event: alerts when the show ends.
        _attach_markers(b.prev['step'], markers, 'end')
        marker_count += len(markers)

    name = info.get('projectTitle') or ''
    if title and title.lower() not in ('default', 'rundown from excel', name.lower()):
        name = f'{name}: {title}' if name else title
    name = name or 'Ontime rundown'
    steps = [s for t in tracks for s in t['steps']]
    description = f'Run of show imported from Ontime: {len(steps)} events'
    if marker_count:
        description += f" and {marker_count} marker{'s' if marker_count > 1 else ''}"
    if b.origin is not None:
        description += f', {_clock(b.origin)[:5]} to {_clock(b.origin + b.length)[:5]}'
    description += '.'

    program = importer.create_base_program(
        name=name, description=description, environment_type='event',
        source_url=source_url, source_type='ontime')
    program['schemaVersion'] = '0.3.0-alpha'
    program['tracks'] = tracks
    meta: Dict[str, Any] = {k: v for k, v in info.items() if v}
    if b.origin is not None:
        meta['showStart'] = _clock(b.origin)
    if b.skipped:
        meta['skippedEvents'] = b.skipped
    if b.delays:
        meta['delaysIgnored'] = b.delays
    program['metadata']['ontime'] = meta
    return program


# ---------------------------------------------------------------------------
# The importer
# ---------------------------------------------------------------------------

class OntimeImporter(BaseImporter):
    """Import an Ontime project file or rundown spreadsheet as a run of show."""

    name = 'ontime'
    description = 'Import an Ontime rundown (project .json, .xlsx, CSV or a pasted table) as a run of show'
    supported_domains: List[str] = []  # files and pasted tables

    def __init__(self, allow_local_files: bool = False):
        # import_from_url() reads a LOCAL path: for the command line and for
        # a file this process wrote itself (an upload). Anything reachable
        # from user input passes the content instead.
        self.allow_local_files = allow_local_files

    def can_import(self, url_or_query: str) -> bool:
        # Ontime has no URL of its own and its files share extensions with
        # other formats, so it is chosen by name, never guessed.
        return False

    def search(self, query: str) -> List[Dict[str, Any]]:
        return []

    def import_from_url(self, url: str, rundown: Optional[str] = None) -> ImportResult:
        if not self.allow_local_files:
            return ImportResult(success=False, source_type='ontime',
                                error='local files are not accepted here; pass the rundown as text')
        path = Path(url)
        if not path.exists():
            return ImportResult(success=False, source_type='ontime', error=f'no such file: {url}')
        return self.import_from_file(str(path), rundown=rundown)

    def import_from_file(self, file_path: str, rundown: Optional[str] = None) -> ImportResult:
        """An Ontime ``.json`` project, an ``.xlsx`` rundown, or a
        ``.csv``/``.tsv``/``.md``/``.txt`` table."""
        path = Path(file_path)
        return self.import_from_bytes(path.read_bytes(), filename=path.name, rundown=rundown)

    def import_from_bytes(self, blob: bytes, *, filename: str = '', rundown: Optional[str] = None,
                          source_url: str = '') -> ImportResult:
        if blob[:2] == b'PK' or filename.lower().endswith('.xlsx'):
            try:
                sheets = _xlsx_sheets(blob)
                return self._from_sheets(sheets, rundown=rundown, source_url=source_url or filename,
                                         title=_title_from_filename(filename))
            except (OntimeFormatError, ElementTree.ParseError, KeyError) as e:
                return ImportResult(success=False, source_type='ontime', error=f'Could not read the workbook: {e}')
        return self.import_from_source(blob.decode('utf-8-sig', errors='replace'), rundown=rundown,
                                       source_url=source_url or filename, title=_title_from_filename(filename))

    def import_from_source(self, source: str, *, rundown: Optional[str] = None,
                           source_url: str = '', title: str = '') -> ImportResult:
        """Text in: a project's JSON, or the rundown as CSV, TSV or a
        Markdown table. A table has no name of its own; ``title`` names it."""
        text = (source or '').lstrip('﻿').strip()
        if not text:
            return ImportResult(success=False, source_type='ontime', error='Nothing to import.')
        try:
            if text[0] in '{[':
                try:
                    data = json.loads(text)
                except json.JSONDecodeError as e:
                    raise OntimeFormatError(f'Not valid JSON: {e}')
                if isinstance(data, list):
                    data = {'rundown': data}
                if not isinstance(data, dict):
                    raise OntimeFormatError('Not an Ontime project.')
                title, entries, info = _entries_from_project(data, rundown)
            else:
                entries, info = _entries_from_rows(_rows_from_text(text)), {}
            program = build_program(title, entries, info, self, source_url)
        except OntimeFormatError as e:
            return ImportResult(success=False, source_type='ontime', error=str(e))
        return ImportResult(success=True, program=program, source_url=source_url or None,
                            source_type='ontime')

    def _from_sheets(self, sheets: List[Tuple[str, List[List[Any]]]], *, rundown: Optional[str],
                     source_url: str, title: str = '') -> ImportResult:
        """The named worksheet, else the first one with a rundown header
        (Ontime's own import does the same)."""
        if rundown:
            sheets = [s for s in sheets if s[0].lower() == rundown.lower()] or sheets
        last_error = 'The workbook has no worksheets.'
        for name, rows in sheets:
            try:
                entries = _entries_from_rows(rows)
                # Ontime's export names the worksheet after the rundown; a
                # spreadsheet app's default name says nothing, the file's does.
                generic = re.fullmatch(r'(sheet|tabelle|feuil|hoja|blad)\s*\d*', name.strip(), re.I)
                program = build_program((title if generic else name) or title, entries,
                                        {'worksheet': name}, self, source_url)
            except OntimeFormatError as e:
                last_error = str(e)
                continue
            return ImportResult(success=True, program=program, source_url=source_url or None,
                                source_type='ontime')
        raise OntimeFormatError(last_error)


def _title_from_filename(filename: str) -> str:
    """'product-launch.csv' -> 'Product launch'."""
    stem = Path(filename or '').stem
    if not stem or stem.startswith('tmp'):
        return ''
    words = re.sub(r'[_\-]+', ' ', stem).strip()
    return words[:1].upper() + words[1:]


def looks_like_ontime_project(data: Any) -> bool:
    """True for the JSON of an Ontime project file, so an upload of one is
    told apart from a Rhylthyme program (which has ``tracks``)."""
    if not isinstance(data, dict) or 'tracks' in data:
        return False
    settings = data.get('settings') if isinstance(data.get('settings'), dict) else {}
    if settings.get('app') == 'ontime':
        return True
    if isinstance(data.get('rundowns'), dict):
        return any(isinstance(r, dict) and isinstance(r.get('entries'), dict) for r in data['rundowns'].values())
    if isinstance(data.get('rundown'), list):
        return any(isinstance(e, dict) and e.get('type') in ('event', 'block', 'delay') for e in data['rundown'])
    return False


ImporterRegistry.register(OntimeImporter())
