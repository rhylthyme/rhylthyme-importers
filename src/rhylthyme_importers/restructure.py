"""
Restructure a flat imported recipe into parallel tracks, with durations read
from the step text. Rule-based, no model.

The bulk recipe importers produce one shape: a "Preparation" track holding a
single "Gather: ..." step, and a "Cooking Steps" track with every step chained
one after another and a per-task placeholder duration. ``restructure`` turns
that into a schedule a cook can follow:

* durations come from the text ("cook 25 minutes", "2 à 3 minutes", "circa
  3-4 minuti", "overnight"); ranges become variable durations. A step with no
  stated time keeps its duration but is marked as an estimate;
* passive steps (rest, marinate, chill, rise, soak) become ``waiting``;
* "Preheat the oven" moves to an Oven track that starts at the beginning, and
  the first oven step waits for it;
* "Bring water to a boil" moves to a Water track, and the step that puts
  pasta (rice, potatoes, ...) in the water waits for it;
* a step that says it happens alongside the previous one ("Meanwhile",
  "While the pasta cooks", "In a separate pan", "Entre-temps", "Nel
  frattempo", ...) runs on its own track, and the next step waits for both;
* the "Gather" step becomes the first step of the main track.

Programs that are not in the bulk-import shape come back unchanged. The caller
validates the result and keeps the original when it does not validate.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Dict, List, Optional, Tuple

VERSION = 1

# ---------------------------------------------------------------------------
# Durations from text

_NUM = r"(\d+(?:[.,]\d+)?|\d*\s*½|\d+\s+1/2)"
_RANGE_SEP = r"\s*(?:-|–|—|to|à|a|o|bis|tot|or)\s*"
_UNITS = [
    (3600, r"h|hrs?|hours?|heures?|ore|ora|stunden?|std\.?|horas?|uur"),
    (60, r"min(?:ute)?s?|mn|minut[oi]|minuten|minutos|minuts"),
    (1, r"sec(?:ond)?s?|secondes?|second[oi]|sekunden?|segundos?|seconden"),
]
_UNIT_RE = "|".join(f"(?P<u{i}>{pat})" for i, (_, pat) in enumerate(_UNITS))
_TIME_RE = re.compile(
    rf"(?<![\w/])(?P<a>{_NUM})(?:{_RANGE_SEP}(?P<b>{_NUM}))?\s*(?:{_UNIT_RE})\b(?=(?P<tail>[^.;]{{0,25}}))",
    re.IGNORECASE,
)
_EVERY_RE = re.compile(r"(every|each|toutes les|ogni|alle|cada|elke)\s*$", re.IGNORECASE)
_PER_SIDE_RE = re.compile(
    r"^\s*(per side|on each side|each side|on both sides|de chaque côté|par côté|per lato|da ogni lato|por lado|pro seite|aan elke kant)",
    re.IGNORECASE,
)
_WORD_TIMES = [
    (re.compile(r"\bovernight\b|toute la nuit|per tutta la notte|über nacht|durante la noche|een nacht", re.I), 8 * 3600),
    (re.compile(r"\bhalf an hour\b|\bhalf hour\b|demi-heure|mezz'?ora|media hora|halbe stunde|half uur", re.I), 1800),
    (re.compile(r"\ban hour\b|\bone hour\b|une heure|un'?ora|una hora|eine stunde|een uur", re.I), 3600),
]


def _number(text: str) -> float:
    text = text.strip().replace(",", ".")
    if "½" in text:
        whole = text.replace("½", "").strip()
        return (float(whole) if whole else 0.0) + 0.5
    if " 1/2" in text:
        return float(text.split()[0]) + 0.5
    return float(text)


# "Will keep chilled for up to 24 hrs", "can be made 2 days ahead": storage, not work.
_KEEPS_RE = re.compile(
    r"(?:keeps?|kept|store[sd]?|last[s]?|will keep|make ahead|made ahead|in advance|freeze[sd]?|"
    r"se conserve|conserver|si conserva|conservare|se conserva|hält sich)\b[^.;]*$",
    re.IGNORECASE,
)


def parse_duration(text: str) -> Optional[Tuple[int, int]]:
    """Seconds a step's text says it takes, as (shortest, longest), or None.

    Every stated time is added up ("simmer 10 minutes, then 5 more" is 15);
    a time "per side" counts twice; "every 30 seconds" is ignored.
    """
    lo = hi = 0.0
    found = False
    for m in _TIME_RE.finditer(text or ""):
        if _EVERY_RE.search(text[: m.start()]) or _KEEPS_RE.search(text[max(0, m.start() - 40): m.start()]):
            continue
        unit = next(_UNITS[i][0] for i in range(len(_UNITS)) if m.group(f"u{i}"))
        try:
            a = _number(m.group("a"))
            b = _number(m.group("b")) if m.group("b") else a
        except ValueError:
            continue
        if b < a:
            a, b = b, a
        times = 2 if _PER_SIDE_RE.search(m.group("tail") or "") else 1
        lo += a * unit * times
        hi += b * unit * times
        found = True
    if not found:
        for pattern, seconds in _WORD_TIMES:
            if pattern.search(text or ""):
                lo = hi = seconds
                found = True
                break
    if not found or hi <= 0:
        return None
    cap = 48 * 3600
    return int(min(lo, cap)), int(min(hi, cap))


# ---------------------------------------------------------------------------
# Kinds of step

_PASSIVE_RE = re.compile(
    r"\b(let (it |them |the \w+ )?(rest|sit|stand|cool|rise|proof|marinate)|rest for|set aside for|"
    r"marinate|chill|refrigerate|rise until|proof|prove|soak|cool (completely|down|slightly)|"
    r"laissez (reposer|refroidir|mariner|lever)|reposer|mariner|réfrigér|"
    r"lasciate (riposare|raffreddare|marinare|lievitare)|riposare|marinare|"
    r"dejar reposar|reposar|marinar|enfriar|ruhen lassen|marinieren|abkühlen|laten rusten|marineren)",
    re.IGNORECASE,
)
_CUE_RE = re.compile(
    r"^\s*(?:\d+[.)]\s*|step \d+:?\s*)?"
    r"(meanwhile|in the meantime|while|at the same time|in a separate|in another|"
    r"entre-temps|entretemps|pendant ce temps|pendant que|dans un autre|dans une autre|"
    r"nel frattempo|intanto|mentre|in un'?altra|in un altro|"
    r"mientras tanto|mientras|en otro|en otra|"
    r"währenddessen|inzwischen|in der zwischenzeit|in einem anderen|in einer anderen|"
    r"ondertussen|intussen|terwijl|enquanto|entretanto|em outra|em outro)\b",
    re.IGNORECASE,
)
_PREHEAT_RE = re.compile(
    r"pre-?heat|heat the oven|préchauff|preriscald|precalient|vorheiz|heizen sie den (back)?ofen|"
    r"verwarm de oven|pré-?aque[cç]",
    re.IGNORECASE,
)
_BOIL_WATER_RE = re.compile(
    r"bring .{0,40}water to (a )?(rolling )?boil|boil (a (large )?pot of )?(salted )?water|"
    r"portate a ebollizione|mettete a bollire|fate bollire|faites bouillir|portez .{0,40}ébullition|"
    r"hervir (el |abundante )?agua|lleve .{0,40}ebullición|wasser zum kochen|"
    r"breng .{0,40}water aan de kook|ferva a água|ferver",
    re.IGNORECASE,
)
_USES_WATER_RE = re.compile(
    r"pasta|spaghetti|noodle|macaroni|penne|linguine|fettuccine|tagliatelle|rigatoni|fusilli|gnocchi|"
    r"\brice\b|\briz\b|\barroz\b|\breis\b|potato|pomme de terre|patate|kartoffel|aardappel|"
    r"into the (boiling )?water|in the (boiling )?water|dans l'eau|nell'acqua|en el agua|ins wasser|"
    r"buttate|scolate|pâtes|nudeln|blanch|dumpling",
    re.IGNORECASE,
)
_OVEN_STEP_RE = re.compile(
    r"\bbake|\broast|in(to)? the oven|enfourne|au four|in forno|al horno|in den ofen|backen|in de oven|no forno",
    re.IGNORECASE,
)
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


def _split_sentence(text: str, pattern: re.Pattern) -> Tuple[Optional[str], str]:
    """The first sentence matching ``pattern``, and the rest of the text."""
    sentences = _SENTENCE_RE.split((text or "").strip())
    for i, sentence in enumerate(sentences):
        if pattern.search(sentence):
            rest = " ".join(sentences[:i] + sentences[i + 1:]).strip()
            return sentence.strip(), rest
    return None, text


def _short_name(text: str, limit: int = 60) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    first = _SENTENCE_RE.split(text)[0]
    return first if len(first) <= limit else first[: limit - 1].rstrip(" ,;:") + "…"


_VERBS = set("""
add adjust allow arrange lay take decorate keep assemble bake baste beat blanch blend blitz boil braise bring broil brown
brush butter carve char check chill chop clean coat combine cook cool core cover crack cream crimp
crumble crush cube cut debone deep-fry deglaze dice dip discard dissolve divide dot drain dredge
drizzle drop dry dust empty fill fillet finish flatten flip fluff fold form freeze frost fry garnish
glaze grate grease grill grind halve heat hull increase insert invert julienne knead ladle layer
leave let lift line load lower make marinate mash massage measure melt microwave mince mix moisten
oil open pack pan-fry pat peel pipe pit place plate poach portion pound pour preheat prepare press
prick process pulse puree purée put quarter reduce refrigerate reheat remove repeat reserve rest
return rinse roast roll rotate rub salt saute sauté scald scatter scoop score scrape sear season
seal separate serve set shake shape shave shell shred shuck sieve sift simmer skewer skim slice
slide smash smooth soak spoon spray spread sprinkle squeeze stamp start steam steep stew stir store
strain stuff submerge sweat swirl taste tear tenderize thaw thicken thread tie tip toast top toss
transfer trim turn uncover unmold unroll wash weigh whip whisk wilt wipe work wrap zest
""".split())
_LEAD_CLAUSE_RE = re.compile(
    r"^(?:once|when|after|while|as soon as|as|if|before|until|meanwhile|in the meantime|to make|"
    r"to serve|to assemble|to finish|to prepare|for|in|on|using|with|working|at|during|from|"
    r"depending|whichever|since|because)\b[^,;:]{0,120}[,;:]\s*",
    re.IGNORECASE,
)
_LEAD_WORD_RE = re.compile(
    r"^(?:then|next|now|finally|first|firstly|lastly|also|simply|carefully|gently|quickly|slowly|"
    r"thoroughly|immediately|just|meanwhile|afterwards|and|to finish|step \d+|you can|you may|you will|you'll|"
    r"you should|you want to|be sure to|make sure to|don't forget to|please)\b[,:]?\s*",
    re.IGNORECASE,
)
_PHRASE_END_RE = re.compile(
    r"[,;.:!?(]|(?<!\d) [-–—] (?!\d)|\b(?:until|so that|so|to form|while|before|making sure|stirring|"
    r"whisking|which|as needed|if needed|if necessary|or until)\b",
    re.IGNORECASE,
)
_TRAILING_RE = re.compile(r"\s+\b(?:the|a|an|and|of|with|to|in|into|on|onto|for|or|at|then|over|your)$",
                          re.IGNORECASE)


# Verbs that are also ingredients or objects: "flour, sugar and salt" isn't a second action.
_NOUNISH = {"salt", "oil", "butter", "zest", "top", "pit", "shell", "quarter", "plate", "spray", "tip",
            "dot", "work", "core", "cube", "fillet", "cream", "glaze", "pipe", "stamp", "press", "dust",
            "set", "rest", "char", "drop", "pan", "pack", "line", "seal", "puree", "purée", "mash"}
_NON_EN_RE = re.compile(r"\b(il|la|le|les|di|del|della|des|du|et|une?|con|per|dans|nel|nella|el|los|las|und|mit|der|die|das)\b",
                        re.IGNORECASE)
_PREP_RE = re.compile(r"^(?:on|onto|in|into|to|with|over|at|from|for|under|through|inside)$", re.IGNORECASE)


_CONTAINER_RE = re.compile(
    r"(?i)(?:in|into|on|using|with) (?:a|an|the|your|one) (?:[\w-]+ ){0,4}?"
    r"(?:bowl|pan|saucepan|skillet|pot|dish|processor|blender|mixer|jug|sheet|tray|board|oven)\s+([^\W\d_]+)"
)


def _base_verb(stem: str) -> str:
    """'preheat' / 'bak' / 'chopp' (a word minus -ing) -> the verb it came from."""
    low = stem.lower()
    for cand in (low, low + "e", low[:-1] if len(low) > 2 and low[-1] == low[-2] else None):
        if cand and cand in _VERBS:
            return cand
    return low


def _is_verb(word: str, foreign: bool) -> bool:
    w = word.lower().strip(",.;:")
    if w in _VERBS:
        return True
    # French and Italian recipes use the plural imperative: ajoutez, tritate, cuocete.
    return foreign and len(w) > 4 and bool(re.search(r"(?:ez|ate|ete|ite)$", w))


def _step_name(text: str, max_words: int = 8) -> str:
    """A short name that starts with what to do.

    "Once you have all your toppings on, transfer the bagels to the oven and
    bake for 25 minutes." -> "Transfer the bagels to the oven".
    """
    text = re.sub(r"\s+", " ", (text or "").strip())
    text = re.sub(r"^\s*(?:step\s*)?\d+\s*[.):-]?\s+|^-\s*", "", text, flags=re.IGNORECASE)
    # "2 tbsp. sugar" isn't the end of a sentence.
    text = re.sub(r"\b(tbsp|tbs|tsp|oz|lb|lbs|approx|pkg|qt|pt|c|g|kg|ml|cl|dl|env)\.", r"\1", text,
                  flags=re.IGNORECASE)
    # "Start by preheating the oven" -> "preheat the oven".
    text = re.sub(r"^\s*(?:start|begin) by ([^\W\d_]+?)(?:ing)\b",
                  lambda m: _base_verb(m.group(1)), text, flags=re.IGNORECASE)
    first = _strip_cue(_SENTENCE_RE.split(text)[0]) if text else ""
    foreign = len(_NON_EN_RE.findall(first)) >= 2
    body = first
    for _ in range(4):
        before = body
        body = _LEAD_WORD_RE.sub("", body)
        if not _is_verb(body.split(" ", 1)[0], foreign):
            # "In a large bowl combine ..." has no comma to end the clause.
            m = _CONTAINER_RE.match(body)
            if m and _is_verb(m.group(1), foreign):
                body = body[m.start(1):]
                continue
            body = _LEAD_CLAUSE_RE.sub("", body)
            if foreign and body == before:
                body = re.sub(r"^[^,;:]{0,60}[,;:]\s*", "", body)
        if body == before:
            break
    if not body or not _is_verb(body.split(" ", 1)[0], foreign):
        # The verb may come later in the sentence: "..., and bake".
        body = None
        for m in re.finditer(r"(?:^|[,;]\s*|\b(?:and|then|et|e|y|und)\s+)([^\W\d_]+)", first):
            if _is_verb(m.group(1), foreign):
                body = first[m.start(1):]
                break
        if body is None:
            return _short_name(text)
    cut = _PHRASE_END_RE.search(body, 1)
    if cut and body[cut.start()] == "," and " " not in body[: cut.start()].strip():
        # "combine, 2 tbsp poppy seeds": a stray comma after the verb.
        body = body[: cut.start()] + body[cut.start() + 1:]
        cut = _PHRASE_END_RE.search(body, 1)
    phrase = body[: cut.start()] if cut else body
    if cut and body[cut.start()] == ",":
        # "Whisk together the flour, sugar and salt": keep a short list whole.
        rest = body[cut.start():]
        m = re.match(r",(?:\s+[^,;.:()]+?,){0,3}\s+[^,;.:()]*?\b(?:and|or|&)\s+"
                     r"(?:(?!(?:in|into|on|onto|to|with|until|then|and|for|over)\b)[^\W\d_]+\s*){1,3}", rest)
        if m and len((phrase + m.group(0)).split()) <= max_words:
            phrase = (phrase + m.group(0)).rstrip()
        elif re.search(r"(?i)\b(?:a|an|the|your)(?: [\w-]+)?$", phrase) and _PREP_RE.pattern:
            # "Transfer the dough to a large, flat surface": drop the half-described place.
            words = phrase.split()
            for i in range(len(words) - 1, 2, -1):
                if _PREP_RE.match(words[i]):
                    phrase = " ".join(words[:i])
                    break
    # A second action ends the name: "Transfer the bagels to the oven and bake".
    for m in re.finditer(r"\s+(?:and|then|&)\s+(?:\w+ly\s+)?([^\W\d_]+)", phrase):
        word = m.group(1).lower()
        gerund = word.endswith("ing") and _base_verb(word[:-3]) in _VERBS   # "... and lining a sheet"
        if (gerund or (_is_verb(word, foreign) and word not in _NOUNISH)) \
                and len(phrase[: m.start()].split()) >= 3:
            phrase = phrase[: m.start()]
            break
    words = phrase.split()
    if len(words) > max_words:
        words = words[:max_words]
        # Stop before a dangling "on a rimmed", keeping at least three words.
        for i in range(len(words) - 1, 2, -1):
            if _PREP_RE.match(words[i]):
                words = words[:i]
                break
        phrase = " ".join(words)
    for _ in range(3):
        phrase = _TRAILING_RE.sub("", phrase.strip())
    phrase = phrase.strip(" ,;:-")
    if not phrase:
        return _short_name(text)
    return phrase[:1].upper() + phrase[1:]


def _strip_cue(text: str) -> str:
    stripped = _CUE_RE.sub("", text or "", count=1).lstrip(" ,:;-")
    return stripped[:1].upper() + stripped[1:] if stripped else text


# ---------------------------------------------------------------------------
# Components: what each step works on

_WORD_RE = re.compile(r"[^\W\d_]{3,}", re.UNICODE)
_STOP = set("""
cup cups tablespoon tablespoons tbsp tbs teaspoon teaspoons tsp ounce ounces pound pounds lbs
gram grams kilogram kilograms litre liter liters litres quart quarts pint pints can cans jar
large small medium fresh freshly chopped minced sliced diced finely roughly thinly whole divided
plus about room temperature optional taste package packet pacchetto pacchetti piece pieces pièce
pièces pinch dash handful cloves clove sprig sprigs stick sticks bunch head heads extra virgin
ground black white leaves leaf more less into with from your that this some other additional
needed serving garnish and the for of or to cut peeled softened melted cold warm hot dried
boneless skinless unsalted salted kosher coarse fine packed light dark style
""".split())
_INTERMEDIATE_RE = re.compile(
    r"\b(dough|batter|sauce|dressing|glaze|filling|topping|crust|marinade|broth|stock|custard|"
    r"syrup|craquelin|meringue|ganache|frosting|icing|salsa|pur[eé]e|pâte|impasto|masa|teig|"
    r"so(ß|ss)e|glaçage|garniture|farce|ripieno|relleno|füllung)\b",
    re.IGNORECASE,
)
_CONTINUE_RE = re.compile(
    r"^\s*(?:\d+[.)]\s*)?(then|once|when|after|afterwards|remove|transfer|return|reduce|continue|"
    r"add|stir in|mix in|whisk in|fold|pour|cook|simmer|bring|season|top|sprinkle|spread|cover|"
    r"ajoutez|incorporez|versez|aggiungete|unite|versate|añada|agregue|añadir|hinzufügen|zugeben|"
    r"repeat|flip|turn|drain|strain|serve|finish|it|they|this|these|the mixture|"
    r"ensuite|puis|retirez|transférez|remettez|servez|dressez|égouttez|poursuivez|"
    r"poi|togliete|trasferite|servite|scolate|continuate|luego|después|retire|sirva|"
    r"dann|danach|anschließend|servieren|daarna|vervolgens|depois)\b",
    re.IGNORECASE,
)


_START_RE = re.compile(
    r"\b(?:in|into)\s+(?:a|another|the other)\s+(?:separate|second|clean|small|medium|large|big|"
    r"medium-sized|mixing)?\s*(?:bowl|saucepan|pan|pot|skillet|jug)|separately|"
    r"^\s*(?:\d+[.)]\s*)?(?:whisk together|mix together|stir together|combine|sift|beat|"
    r"peel|chop|dice|slice|grate|trim|wash|rinse|soak|marinate|put|place|"
    r"dans un (?:autre )?(?:saladier|bol|récipient)|in una (?:ciotola|terrina)|en un (?:bol|cuenco))\b",
    re.IGNORECASE,
)


def _stem(word: str) -> str:
    word = word.lower()
    if len(word) > 4 and word.endswith(("oes", "shes", "ches")):
        return word[:-2]
    if len(word) > 4 and word.endswith("s") and not word.endswith(("ss", "us")):
        return word[:-1]
    return word


_HEADER_RE = re.compile(
    r"^\s*(?:(?:to\s+)?make\s+the|for\s+(?:the\s+)?|prepare\s+the|pour\s+(?:la|le|les)|per\s+(?:la|il|lo)|"
    r"para\s+(?:la|el)|für\s+(?:den|die|das))?\s*(?P<label>[^:.!?]{2,50}):\s*$",
    re.IGNORECASE,
)


def _header_label(text: str) -> Optional[str]:
    """'Make the buttercream filling:' -> 'Buttercream filling'.

    Returns "" for a header that isn't a component ("For a gas grill:", "Notes:"),
    None for a step that isn't a header.
    """
    m = _HEADER_RE.match(text or "")
    if not m:
        short = (text or "").strip()
        # "For the lemon vinaigrette", "Tips & Notes": a heading without the colon.
        if short and len(short.split()) <= 5 and not re.search(r"[.!?]$", short) and \
                (re.match(r"(?i)(for (the |a |an )?\w|tips?\b|notes?\b)", short)) and \
                not _is_verb(short.split()[0], False):
            return _header_label(short + ":")
        return None
    label = m.group("label").strip()
    if re.match(r"(?i)(for an?\s|if\s|or\s|option|alternative|using\s|on the stove|in the oven|"
                r"notes?|tips?|variations?|adjusting|serving|storage|to serve|nutrition|"
                r"remarques?|conseils?|note|consigli|notas?|hinweise?)\b", label):
        return ""
    return label[:1].upper() + label[1:]


def ingredient_tokens(program: Dict[str, Any], gather: Dict[str, Any]) -> set:
    """Significant words of the recipe's ingredient names."""
    names = [i.get("name", "") for i in ((program.get("metadata") or {}).get("ingredients") or [])
             if isinstance(i, dict)]
    if not names:
        text = (gather.get("description") or "")
        names = text.split(":", 1)[-1].split(",")
    tokens = set()
    for name in names:
        name = re.sub(r"\([^)]*\)", " ", name)
        for w in _WORD_RE.findall(name):
            w = _stem(w)
            if len(w) >= 4 and w not in _STOP:
                tokens.add(w)
    return tokens


def step_tokens(text: str, known: set) -> set:
    """The ingredients and intermediates (sauce, dough, ...) a step mentions."""
    words = {_stem(w) for w in _WORD_RE.findall(text or "")}
    found = words & known
    found |= {m.group(1).lower() for m in _INTERMEDIATE_RE.finditer(text or "")}
    return found


# ---------------------------------------------------------------------------
# Restructuring


def _is_bulk_shape(program: Dict[str, Any]) -> Tuple[Optional[Dict], Optional[Dict]]:
    tracks = program.get("tracks") or []
    if len(tracks) != 2:
        return None, None
    prep, cooking = tracks
    prep_steps = prep.get("steps") or []
    if (prep.get("name") != "Preparation" or len(prep_steps) != 1
            or not (cooking.get("steps") or [])):
        return None, None
    return prep_steps[0], cooking


def _set_duration(step: Dict[str, Any], text: str, counts: Dict[str, int]) -> None:
    parsed = parse_duration(text)
    if parsed:
        lo, hi = parsed
        if lo == hi:
            step["duration"] = {"type": "fixed", "seconds": hi}
        else:
            step["duration"] = {"type": "variable", "minSeconds": lo, "maxSeconds": hi,
                                "defaultSeconds": (lo + hi) // 2}
        (step.get("metadata") or {}).pop("durationEstimate", None)
        counts["timed"] += 1
    else:
        step.setdefault("metadata", {})["durationEstimate"] = True
        counts["estimated"] += 1


_IN_OVEN_RE = re.compile(
    r"(?:^|[.;:!]\s*|\b(?:and|then|or|to|&)\s+)(?:bake|roast|broil|grill|barbecue)\b|"
    r"\b(?:on|onto) the (?:pre-?heated |hot )?(?:grill|barbecue|bbq)|"
    r"\b(?:in(?:to)? the (?:pre-?heated |hot )?oven|enfournez|au four|"
    r"in forno|al horno|in den (?:vorgeheizten )?ofen|backen|in de oven|no forno)\b",
    re.IGNORECASE,
)
_ON_STOVE_RE = re.compile(
    r"\b(saucepan|skillet|frying pan|sauté pan|stockpot|dutch oven|wok|pot|griddle|"
    r"over (?:very |a )?(?:low|medium|high|medium-high|medium-low|moderate) heat|simmer\w*|boil\w*|"
    r"saut[ée]\w*|fry|fries|fried|sear\w*|poach\w*|deglaze\w*|steam\w*|brown the|"
    r"casserole|poêle|faites (?:revenir|cuire|fondre)|padella|pentola|sartén|cazuela|olla|"
    r"topf|pfanne|koekenpan)\b",
    re.IGNORECASE,
)


def _equipment(step: Dict[str, Any]) -> Optional[str]:
    """'oven' or 'stovetop' from what the step's text says, else None."""
    text = step.get("description") or step.get("name") or ""
    text = re.sub(r"(?i)\b(baking|roasting) (sheet|dish|pan|tray|tin|paper|powder|soda)s?\b|"
                  r"\bfrom the oven\b|\bdutch oven\b|\boven[- ]proof\b|\bdeep[- ]fry(?:ing)? thermometer\b", " ", text)
    oven, stove = _IN_OVEN_RE.search(text), _ON_STOVE_RE.search(text)
    if oven and (not stove or oven.start() < stove.start()):
        return "oven"
    return "stovetop" if stove else None


_FOLLOW_RE = re.compile(r"^\s*(?:\d+[.)]\s*)?(flip|turn|baste|rotate|continue|repeat|retournez|girate|voltee|wenden)\b",
                        re.IGNORECASE)


_ALT_RE = re.compile(
    r"^\s*(?:(?:to cook |to make it |cook(?:ing)? )?(?:for|on|in|with|using|if using|if you(?:'re| are)? using|"
    r"if you have|or|alternatively,?|to cook)\s+(?:a|an|the|your)?\s*)"
    r"(?P<label>(?:gas|charcoal|electric|outdoor|indoor|stovetop|stove|oven|grill|barbecue|bbq|skillet|pan|"
    r"grill pan|griddle|broiler|air fryer|slow cooker|pressure cooker|instant pot|multicooker|microwave|"
    r"smoker|sous vide|dutch oven|cast[- ]iron|campfire|wood[- ]fired|pellet)[\w\s-]{0,25}?)"
    r"\s*(?:method|version|option|instructions|directions)?\s*:?\s*$",
    re.IGNORECASE,
)


def _slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "option"


def _alternative_label(text: str) -> Optional[str]:
    """'For a gas grill:' -> 'Gas grill'; None when the line isn't an alternative method."""
    short = re.sub(r"\s+", " ", (text or "").strip())
    if not short or len(short.split()) > 8 or re.search(r"[.!?]$", short):
        return None
    m = _ALT_RE.match(short)
    if not m:
        return None
    label = m.group("label").strip()
    return label[:1].upper() + label[1:]


def _find_alternatives(steps: List[Dict[str, Any]]) -> Dict[int, List[Tuple[str, List[int]]]]:
    """Runs of two or more method headers ("For a gas grill:", "For a skillet:"),
    keyed by the index of the first header. Each option owns the steps up to the
    next header; the last option takes no more steps than the longest before it,
    so the steps after the alternatives stay common."""
    texts = [s.get("description") or s.get("name") or "" for s in steps]
    groups: Dict[int, List[Tuple[str, List[int]]]] = {}
    i = 0
    while i < len(steps):
        label = _alternative_label(texts[i])
        if label is None:
            i += 1
            continue
        start, options, j = i, [], i
        while j < len(steps) and (lab := _alternative_label(texts[j])) is not None:
            k = j + 1
            while k < len(steps) and _header_label(texts[k]) is None and _alternative_label(texts[k]) is None:
                k += 1
            options.append((lab, list(range(j + 1, k))))
            j = k
        if len(options) >= 2 and all(idx for _, idx in options[:-1]):
            longest = max(len(idx) for _, idx in options[:-1])
            options[-1] = (options[-1][0], options[-1][1][:longest])
            if options[-1][1]:
                groups[start] = options
        i = max(j, i + 1)
    return groups


def _group_by_equipment(tracks: List[Dict[str, Any]], order: Dict[str, int],
                        counts: Dict[str, int]) -> List[Dict[str, Any]]:
    """Put every oven step on the Oven track and the main track's stovetop steps on a
    Stovetop track, so each track is one place in the kitchen. A moved step also
    waits for the step before it on its new track: one oven, one cook at the stove."""
    main = tracks[0]
    oven = next((t for t in tracks if t["trackId"] == "oven"), None)
    moves = []   # (step, destination)
    prev_step = None
    for t in tracks:
        if t["trackId"] == "oven" or t.get("branch"):
            continue
        for step in t["steps"]:
            if step is main["steps"][0] or step["stepId"] not in order:
                continue   # Gather, and steps split off earlier (preheat, water)
            kind = _equipment(step)
            if kind is None and moves and moves[-1][0] is prev_step and \
                    _FOLLOW_RE.search(step.get("description") or step.get("name") or ""):
                kind = moves[-1][1]   # "Flip the burgers" stays at the grill
            prev_step = step
            if kind == "oven":
                step["task"] = "oven"
                moves.append((step, "oven"))
            elif t is main and kind == "stovetop" and step.get("task") != "waiting":
                step["task"] = "stove-burner"
                moves.append((step, "stovetop"))
    if not moves:
        return tracks
    if oven is None and any(d == "oven" for _, d in moves):
        oven = {"trackId": "oven", "name": "Oven", "steps": []}
        tracks.insert(1, oven)
    stove = None
    if any(d == "stovetop" for _, d in moves):
        stove = {"trackId": "stovetop", "name": "Stovetop", "steps": []}
        tracks.insert(1, stove)
    moved = {id(s) for s, _ in moves}
    for t in tracks:
        if t is not oven and t is not stove:
            t["steps"] = [s for s in t["steps"] if id(s) not in moved]
    for step, dest in sorted(moves, key=lambda m: order.get(m[0]["stepId"], 10**6)):
        target = oven if dest == "oven" else stove
        if target["steps"]:
            prev = target["steps"][-1]["stepId"]
            trig = step["startTrigger"]
            existing = trig["triggers"] if "logic" in trig else [trig]
            if prev not in {x.get("stepId") for x in existing}:
                step["startTrigger"] = {"logic": "all", "triggers":
                                        existing + [{"type": "afterStep", "stepId": prev}]}
        target["steps"].append(step)
        counts[dest] = counts.get(dest, 0) + 1
    return [t for t in tracks if t["steps"]]


def restructure(program: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Return (program, report). ``program`` is never mutated; a program not
    in the bulk-import shape is returned unchanged with ``report['changed']``
    False and a reason."""
    gather, cooking = _is_bulk_shape(program)
    if gather is None:
        return program, {"changed": False, "reason": "not in the bulk-import shape"}

    out = copy.deepcopy(program)
    counts = {"timed": 0, "estimated": 0, "passive": 0, "parallel": 0, "component": 0, "header": 0, "preheat": 0, "water": 0}
    main: List[Dict[str, Any]] = []
    side_tracks: List[Dict[str, Any]] = []
    ids = set()

    def new_id(base: str) -> str:
        i, candidate = 1, base
        while candidate in ids:
            i += 1
            candidate = f"{base}_{i}"
        ids.add(candidate)
        return candidate

    for s in cooking["steps"]:
        ids.add(s["stepId"])
    ids.add(gather["stepId"])

    first = copy.deepcopy(gather)
    first["name"] = "Gather ingredients"
    first["startTrigger"] = {"type": "programStart"}
    main.append(first)

    known = ingredient_tokens(program, gather)
    # Components: chains of steps working on the same things. Chain 0 starts
    # with Gather; a new chain starts after Gather.
    chains: List[Dict[str, Any]] = [{"steps": main, "tokens": set(), "label": None}]
    current = 0
    oven_ready: Optional[str] = None
    water_ready: Optional[str] = None
    gather_id = first["stepId"]

    def trigger_after(chain: Dict[str, Any], extra: List[str]) -> Dict[str, Any]:
        prev_id = chain["steps"][-1]["stepId"] if chain["steps"] else gather_id
        waits = [prev_id] + [x for x in extra if x and x != prev_id]
        if len(waits) == 1:
            return {"type": "afterStep", "stepId": waits[0]}
        return {"logic": "all", "triggers": [{"type": "afterStep", "stepId": x} for x in waits]}

    pending_label: Optional[str] = None
    alternatives = _find_alternatives(cooking["steps"])
    in_branch = {i for opts in alternatives.values() for lab, idx in opts for i in idx}
    in_branch |= {i for start, opts in alternatives.items()
                  for i in range(start, max(idx[-1] if idx else start for _, idx in opts) + 1)}
    branch_tracks: List[Dict[str, Any]] = []
    after_branches: List[str] = []   # the next step waits for whichever option ran
    for index, original in enumerate(cooking["steps"]):
        if index in alternatives:
            # "For a gas grill: ... For a skillet: ...": a choice step, one track per option.
            options = alternatives[index]
            choice_step = {
                "stepId": new_id(f"choose_method_{index + 1:02d}"),
                "name": "Choose: " + " or ".join(lab.lower() for lab, _ in options),
                "description": "Pick one way to cook this; only that option's steps run.",
                "task": "prep-work",
                "duration": {"type": "fixed", "seconds": 0},
                "choice": {"prompt": "Which method are you using?",
                           "options": [{"choiceId": _slug(lab), "label": lab} for lab, _ in options]},
            }
            # The method is chosen up front, right after Gather, so an option's
            # preheat can start early; its cooking still waits for the prep.
            choice_step["startTrigger"] = {"type": "afterStep", "stepId": gather_id}
            main.insert(1, choice_step)
            prep_done = chains[current]["steps"][-1]["stepId"]
            if after_branches:   # a second set of alternatives follows the first
                prep_done = after_branches[0] if len(after_branches) == 1 else prep_done
                after_branches = []
            ends = []
            for lab, idx in options:
                cid = _slug(lab)
                on_choice = {"type": "afterStep", "stepId": choice_step["stepId"], "choiceId": cid}
                prev_id = None
                branch_steps = []
                pieces = []
                for i in idx:
                    b = copy.deepcopy(cooking["steps"][i])
                    btext = b.get("description") or b.get("name") or ""
                    heat, rest = _split_sentence(btext, _PREHEAT_RE)
                    if heat:
                        pieces.append(({"stepId": new_id(f"preheat_{cid}"), "task": "oven",
                                        "description": heat}, heat, True))
                        if len(rest) < 5:
                            continue
                        b["description"] = btext = rest
                    pieces.append((b, btext, False))
                waited_for_prep = False
                for b, btext, is_heat in pieces:
                    b["name"] = _step_name(btext)
                    if is_heat:
                        parsed = parse_duration(btext)
                        b["duration"] = {"type": "fixed", "seconds": parsed[1] if parsed else 600}
                        if not parsed:
                            b["metadata"] = {"durationEstimate": True}
                        counts["preheat"] += 1
                    else:
                        _set_duration(b, btext, counts)
                        kind = _equipment(b)
                        if _PASSIVE_RE.search(btext) and kind is None:
                            b["task"] = "waiting"
                            counts["passive"] += 1
                        elif kind:
                            b["task"] = "oven" if kind == "oven" else "stove-burner"
                    waits = [on_choice] if prev_id is None else [{"type": "afterStep", "stepId": prev_id}]
                    if not is_heat and not waited_for_prep:
                        waits.append({"type": "afterStep", "stepId": prep_done})
                        waited_for_prep = True
                    b["startTrigger"] = waits[0] if len(waits) == 1 else {"logic": "all", "triggers": waits}
                    b.setdefault("metadata", {})["choiceId"] = cid
                    prev_id = b["stepId"]
                    branch_steps.append(b)
                if branch_steps:
                    ends.append(prev_id)
                    branch_tracks.append({"trackId": new_id(f"option_{cid}"), "name": lab,
                                          "branch": True, "steps": branch_steps})
            after_branches = ends
            counts["choice"] = counts.get("choice", 0) + 1
            continue
        if index in in_branch:
            continue
        step = copy.deepcopy(original)
        text = step.get("description") or step.get("name") or ""

        # A section header names the component that follows; it isn't a step.
        header = _header_label(text)
        if header is not None:
            title = (program.get("name") or "").strip().lower()
            if header and header.lower().rstrip(" recipe") != title.rstrip(" recipe") \
                    and header.lower() not in title:
                pending_label = header
                counts["header"] = counts.get("header", 0) + 1
            continue

        # Preheat the oven: its own track from the start.
        preheat, rest = _split_sentence(text, _PREHEAT_RE)
        if preheat and oven_ready is None:
            oven_step = {
                "stepId": new_id("preheat_oven"), "name": _step_name(preheat),
                "description": preheat, "task": "oven",
                "startTrigger": {"type": "programStart"},
            }
            parsed = parse_duration(preheat)
            oven_step["duration"] = {"type": "fixed", "seconds": parsed[1] if parsed else 600}
            if not parsed:
                oven_step["metadata"] = {"durationEstimate": True}
            oven_name = "Grill" if re.search(r"(?i)grill|barbe|bbq|griglia|parrilla", preheat) else "Oven"
            side_tracks.append({"trackId": "oven", "name": oven_name, "steps": [oven_step]})
            oven_ready = oven_step["stepId"]
            counts["preheat"] += 1
            if len(rest) < 5:
                continue
            text = rest
            step["description"] = rest
            step["name"] = _short_name(rest)

        # Boil water: its own track; the step that uses the water waits.
        boil, rest = _split_sentence(text, _BOIL_WATER_RE)
        if boil and water_ready is None:
            water_step = {
                "stepId": new_id("boil_water"), "name": _step_name(boil),
                "description": boil, "task": "stove-burner",
                "startTrigger": {"type": "programStart"},
            }
            parsed = parse_duration(boil)
            water_step["duration"] = {"type": "fixed", "seconds": parsed[1] if parsed else 600}
            if not parsed:
                water_step["metadata"] = {"durationEstimate": True}
            side_tracks.append({"trackId": "water", "name": "Water", "steps": [water_step]})
            water_ready = water_step["stepId"]
            counts["water"] += 1
            if len(rest) < 5:
                continue
            text = rest
            step["description"] = rest
            step["name"] = _short_name(rest)

        step["name"] = _step_name(text)
        _set_duration(step, text, counts)
        if _PASSIVE_RE.search(text) and step.get("task") not in ("oven", "stove-burner"):
            step["task"] = "waiting"
            counts["passive"] += 1

        toks = step_tokens(text, known)
        has_real_steps = len(main) > 1 or len(chains) > 1
        extra: List[str] = []
        if oven_ready and oven_ready != "done" and (step.get("task") == "oven" or _OVEN_STEP_RE.search(text)):
            extra.append(oven_ready)
            oven_ready = "done"
        if water_ready and water_ready != "done" and _USES_WATER_RE.search(text):
            extra.append(water_ready)
            water_ready = "done"

        if _CUE_RE.search(text) and has_real_steps:
            # Alongside the previous step: a new component, starting with it.
            step["name"] = _step_name(text)
            prev_step = chains[current]["steps"][-1]
            step["startTrigger"] = copy.deepcopy(prev_step["startTrigger"])
            chains.append({"steps": [step], "tokens": set(toks), "label": None, "text": text})
            current = len(chains) - 1
            counts["parallel"] += 1
            continue

        linked = [i for i, c in enumerate(chains) if c["tokens"] & toks]
        seen = set().union(*(c["tokens"] for c in chains))
        fresh = {t for t in toks - seen if t in known}
        label_here, pending_label = pending_label, None
        if label_here and has_real_steps:
            # A new section: its own component, starting after Gather.
            chains.append({"steps": [], "tokens": set(), "label": label_here, "text": text})
            counts["component"] = counts.get("component", 0) + 1
            target, joins = len(chains) - 1, [i for i in linked if i != len(chains) - 1]
        elif label_here and not chains[current]["label"]:
            chains[current]["label"] = label_here
            target, joins = current, [i for i in linked if i != current]
        elif _CONTINUE_RE.search(text) or not toks:
            target, joins = current, [i for i in linked if i != current]
        elif not linked or (len(fresh) >= 2 and current not in linked):
            if has_real_steps and chains[current]["tokens"] and len(fresh) >= 2 and _START_RE.search(text):
                chains.append({"steps": [], "tokens": set(), "label": None, "text": text})
                counts["component"] = counts.get("component", 0) + 1
                target, joins = len(chains) - 1, linked
            else:
                target, joins = current, [i for i in linked if i != current]
        else:
            target, joins = min(linked), [i for i in linked if i != min(linked)]

        chain = chains[target]
        extra += [chains[j]["steps"][-1]["stepId"] for j in joins if chains[j]["steps"]]
        step["startTrigger"] = trigger_after(chain, extra)
        if after_branches:
            # Only one option runs, so wait for whichever finishes (compound
            # triggers don't nest, so other waits carry over to later steps).
            step["startTrigger"] = ({"type": "afterStep", "stepId": after_branches[0]}
                                    if len(after_branches) == 1 else
                                    {"logic": "any", "triggers": [{"type": "afterStep", "stepId": x}
                                                                  for x in after_branches]})
            target, current = current, current
            chain = chains[current]
            after_branches = []
        chain["steps"].append(step)
        chain["tokens"] |= toks
        for j in joins:
            chain["tokens"] |= chains[j]["tokens"]
        chain.setdefault("text", text)
        current = target

    # The final step waits for every component, and for water nobody used.
    last_chain = chains[current]
    if last_chain["steps"]:
        last = last_chain["steps"][-1]
        waits = [c["steps"][-1]["stepId"] for c in chains if c is not last_chain and c["steps"]]
        if water_ready and water_ready != "done":
            waits.append(water_ready)
        prior = last["startTrigger"]
        existing = prior["triggers"] if "logic" in prior else [prior]
        have = {t.get("stepId") for t in existing}
        more = [{"type": "afterStep", "stepId": x} for x in waits if x not in have and x != last["stepId"]]
        if more:
            last["startTrigger"] = {"logic": "all", "triggers": existing + more}

    ingredient_names = [
        re.sub(r"^[\d\s/½¼¾.,-]+", "", re.sub(r"\([^)]*\)|,.*$", "", i.get("name", ""))).strip().lower()
        for i in ((program.get("metadata") or {}).get("ingredients") or []) if isinstance(i, dict)
    ]

    _LABEL_SKIP = {"zested", "zest", "juiced", "stalk", "stalks", "cup", "cups", "mashed", "each", "all-purpose", "beaten", "softened", "cubed",
                   "shredded", "crumbled", "toasted", "cooked", "uncooked", "frozen", "canned"}

    _SEASONING = {"salt", "pepper", "water", "sugar", "butter", "flour", "garlic", "spray", "stock"}

    def ingredient_label(word: str) -> str:
        """'sour' -> 'Sour cream' when an ingredient is named that way."""
        low = word.lower()
        for name in sorted(ingredient_names, key=len):
            words = [w for w in name.split() if _stem(w) not in _STOP and w not in _LABEL_SKIP]
            if low in words and len(words) <= 3:
                name = " ".join(words)
                return name[:1].upper() + name[1:]
        return word.capitalize()

    def label(chain: Dict[str, Any], fallback: str) -> str:
        if chain.get("label"):
            return chain["label"]
        text = chain.get("text") or ""
        m = _INTERMEDIATE_RE.search(text)
        if m:
            return m.group(1).capitalize()
        words = _WORD_RE.findall(text)
        hits = [i for i, x in enumerate(words)
                if _stem(x) in chain["tokens"] and x.lower() not in _LABEL_SKIP]
        hits = [i for i in hits if words[i].lower() not in _SEASONING] or hits
        if not hits:
            return fallback
        i = hits[0]
        pair = " ".join(words[i:i + 2]).lower()
        if i + 1 < len(words) and any(pair in n for n in ingredient_names):
            return pair[:1].upper() + pair[1:]
        return words[i].capitalize()

    used_names = {"Cooking", "Oven", "Water"}
    for i, chain in enumerate(chains[1:], start=1):
        if chain["steps"]:
            name = label(chain, _short_name(chain.get("text", ""), 40))
            base, n = name, 2
            while name in used_names:
                name, n = f"{base} ({n})", n + 1
            used_names.add(name)
            side_tracks.append({"trackId": new_id(f"component_{i}"), "name": name,
                                "steps": chain["steps"]})

    main_track = {"trackId": cooking.get("trackId", "cooking"), "name": chains[0].get("label") or "Prep",
                  "description": cooking.get("description", ""), "steps": main}
    order = {s["stepId"]: i for i, s in enumerate(cooking["steps"])}
    out["tracks"] = _group_by_equipment([main_track] + side_tracks + branch_tracks, order, counts)
    for t in out["tracks"]:
        t.pop("branch", None)
    if counts.get("choice") and str(out.get("schemaVersion", "0.1.0")) < "0.2.0-alpha":
        out["schemaVersion"] = "0.2.0-alpha"
    report = {"changed": True, "version": VERSION, **counts}
    out.setdefault("metadata", {})["restructured"] = {"version": VERSION, "rules": counts}
    return out, report
