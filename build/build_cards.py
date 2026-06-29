#!/usr/bin/env python3
"""
Build cards.json for the Wheelock vocab game.

Sources:
  - ../wheelock_list.rtf : authoritative chapters + part-of-speech + structure (NO macrons)
  - ./quia.html          : macrons, encoded as circumflex vowels (â ê î ô û == ā ē ī ō ū)

Strategy:
  RTF is the backbone. For each RTF entry we try to pull a fully-macronized citation
  form from Quia by matching on a normalized (macron-stripped, de-annotated) headword.
  Matched  -> macronized citation comes from Quia (trusted).
  Unmatched-> rule-based best-guess macronization + flagged for human review.

Outputs:
  ../cards.json        : full card dataset for the app
  ../macron_review.tsv : every entry needing a human macron check, with a guess column
"""
import re, html, json, unicodedata, subprocess, sys, os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RTF  = os.path.join(ROOT, "wheelock_list.rtf")
QUIA = os.path.join(HERE, "quia.html")

CIRC = str.maketrans("âêîôûÂÊÎÔÛ", "āēīōūĀĒĪŌŪ")

def strip_macrons(s):
    return "".join(c for c in unicodedata.normalize("NFD", s)
                    if unicodedata.category(c) != "Mn")

MAC = {"a": "ā", "e": "ē", "i": "ī", "o": "ō", "u": "ū",
       "A": "Ā", "E": "Ē", "I": "Ī", "O": "Ō", "U": "Ū"}

def transfer_macrons(plain, macron):
    """Overlay the macron pattern of `macron` onto the same letters in `plain`.
    Returns `plain` unchanged if the two don't share an identical letter sequence."""
    mac = unicodedata.normalize("NFC", macron)
    if strip_macrons(mac).lower() != plain.lower() or len(mac) != len(plain):
        return plain
    out = []
    for ch, mc in zip(plain, mac):
        out.append(MAC.get(ch, ch) if strip_macrons(mc) != mc else ch)
    return "".join(out)

def norm_head(token):
    """Normalize one citation token to a match key: lowercase, drop macrons,
    drop parenthetical annotations and trailing punctuation."""
    token = re.sub(r"\([^)]*\)", "", token)      # (+ abl.), (idiom)...
    token = strip_macrons(token).lower()
    token = re.sub(r"[?.!]", "", token)
    token = re.sub(r"\s+", " ", token).strip()
    return token

def head_key(latin):
    """The single match key for a citation form: its headword (first token),
    normalized (lowercase, macron-stripped, de-annotated)."""
    first = latin.split(",")[0]
    return norm_head(first)

def pos_family(pos):
    """Collapse an RTF part-of-speech label to a coarse family for match guarding."""
    if pos.startswith("noun"):      return "noun"
    if pos.startswith("verb"):      return "verb"
    if pos.startswith("adjective"): return "adj"
    return "other"

def infer_quia_family(term):
    """Guess a coarse POS family from a Quia citation string's shape, so a
    headword shared across parts of speech (amīcus noun vs adj) matches correctly."""
    has_gender = bool(re.search(r"\b[mfn]\.", term))
    has_inf    = bool(re.search(r"\w(āre|ēre|ere|īre)\b", term))
    if re.search(r",\s*-?\w*a,\s*-?\w*um", term) or re.search(r"\b\w+is,\s*\w*e\b", term):
        return "adj"          # x, -a, -um   or   fortis, forte
    if has_inf and not has_gender:
        return "verb"
    if has_gender:
        return "noun"
    return "other"

# ---------------------------------------------------------------- parse Quia
def parse_quia():
    raw = open(QUIA, encoding="utf-8").read()
    rows = re.findall(r"<tr[^>]*>\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>\s*</tr>",
                      raw, re.S)
    def clean(s):
        s = re.sub(r"<[^>]+>", " ", s)
        s = html.unescape(s).translate(CIRC)
        return re.sub(r"\s+", " ", s).strip()
    index = {}      # match key -> list of (macronized term, inferred family)
    for t, d in rows:
        term = clean(t)
        if not term:
            continue
        k = head_key(term)
        if k:
            index.setdefault(k, []).append((term, infer_quia_family(term)))
    return index

# ---------------------------------------------------------------- parse RTF
def parse_rtf():
    txt = subprocess.run(["textutil", "-convert", "txt", "-stdout", RTF],
                         capture_output=True, text=True, check=True).stdout
    lines = txt.split("\n")
    # header is: Chapter / Part of Speech / Latin / English
    body = lines[4:]
    recs = []
    i = 0
    while i + 3 < len(body):
        chap = body[i].strip()
        if not chap.isdigit():
            i += 1
            continue
        recs.append({
            "chapter": int(chap),
            "pos":     body[i+1].strip(),
            "latin":   body[i+2].strip(),
            "english": body[i+3].strip(),
        })
        i += 4
    return recs

def load_overrides():
    """Hand-filled macron corrections, keyed by card id (build/macron_overrides.tsv)."""
    path = os.path.join(HERE, "macron_overrides.tsv")
    ov = {}
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            p = line.split("\t")
            if len(p) >= 2 and p[0].strip():
                ov[p[0].strip()] = p[1].strip()
    return ov

def load_splits():
    """Idioms / distinct plural senses to split into their own cards, keyed by
    parent card id (build/splits.tsv)."""
    path = os.path.join(HERE, "splits.tsv")
    sp = {}
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            p = line.split("\t")
            if len(p) >= 4:
                sp.setdefault(p[0].strip(), []).append(
                    {"kind": p[1].strip(), "latin": p[2].strip(), "english": p[3].strip()})
    return sp

def strip_last_paren(s):
    """Remove the final balanced parenthetical group (an idiom note) from a gloss."""
    s = s.rstrip()
    if not s.endswith(")"):
        return s
    depth = 0
    for i in range(len(s) - 1, -1, -1):
        if   s[i] == ")": depth += 1
        elif s[i] == "(": depth -= 1
        if depth == 0:
            return s[:i].rstrip()
    return s

def strip_plural_sense(s):
    """Remove a trailing '; (plural) ...' clause from a gloss."""
    return re.sub(r";?\s*\((?:in )?plural\).*$", "", s, flags=re.I).strip()

def expand_noun_genitive(latin):
    """Dictionary entries abbreviate the genitive ('fīlia, -ae, f.'). Expand it to
    the full form ('fīlia, fīliae, f.') so the English→Latin answer is the real word."""
    parts = [p.strip() for p in latin.split(",")]
    if len(parts) < 2 or not parts[1].startswith("-"):
        return latin
    nom, end = parts[0], parts[1]
    gen = None
    if   end == "-ae"  and nom.endswith("a"):                       gen = nom[:-1] + "ae"
    elif end in ("-ī","-iī","-ii") and (nom.endswith("us") or nom.endswith("um")): gen = nom[:-2] + "ī"
    elif end == "-ārum" and nom.endswith("ae"):                     gen = nom[:-2] + "ārum"
    elif end in ("-ōrum","-orum") and nom.endswith("a"):           gen = nom[:-1] + "ōrum"
    elif end in ("-ōrum","-orum") and nom.endswith("ī"):           gen = nom[:-1] + "ōrum"
    elif end in ("-ūs","-us") and nom.endswith("us"):              gen = nom[:-2] + "ūs"
    if gen:
        parts[1] = gen
        return ", ".join(parts)
    return latin

def expand_verb_prefix(first, token):
    """A compound verb abbreviates shared letters with a leading hyphen, e.g.
    'inveniō, …, -vēnī' → 'invēnī'. Recover the prefix by aligning the hyphenated
    stem against the first principal part (longest-overlap match)."""
    tail = token[1:]
    fs, ts = strip_macrons(first), strip_macrons(tail)
    best_i, best_len = 1, -1
    for i in range(1, len(fs)):
        l = 0
        while i + l < len(fs) and l < len(ts) and fs[i + l] == ts[l]:
            l += 1
        if l >= best_len:           # on a tie prefer the longer prefix (handles accipiō → accēpī)
            best_len, best_i = l, i
    return (first[:best_i] + tail) if best_len >= 1 else token

def expand_gen_ending(nom, end):
    if end == "-ae"  and nom.endswith("a"):                            return nom[:-1] + "ae"
    if end in ("-ī","-iī","-ii") and (nom.endswith("us") or nom.endswith("um")): return nom[:-2] + "ī"
    if end == "-ārum" and nom.endswith("ae"):                          return nom[:-2] + "ārum"
    if end in ("-ōrum","-orum") and nom.endswith("a"):                 return nom[:-1] + "ōrum"
    if end in ("-ōrum","-orum") and nom.endswith("ī"):                 return nom[:-1] + "ōrum"
    if end in ("-ūs","-us") and nom.endswith("us"):                    return nom[:-2] + "ūs"
    return end

def spell_out(pos, latin):
    """Expand all abbreviated forms to full words: adjective endings, compound-verb
    hyphen-prefixes, and noun genitives (incl. combined m./f. entries)."""
    parts = [p.strip() for p in latin.split(",")]
    if pos.startswith("adjective"):
        masc = parts[0]
        stem = masc[:-2] if masc.endswith("us") else masc[:-1] if masc.endswith("ī") else None
        if stem is None:
            return latin
        return ", ".join([masc] + [(stem + p[1:]) if (p.startswith("-") and p != "-") else p
                                   for p in parts[1:]])
    if pos.startswith("verb"):
        first = parts[0]
        return ", ".join([first] + [expand_verb_prefix(first, p) if (p.startswith("-") and p != "-") else p
                                    for p in parts[1:]])
    if pos.startswith("noun"):
        out, last = [], None
        for p in parts:
            if p.startswith("-") and p != "-" and last:
                out.append(expand_gen_ending(last, p))
            else:
                out.append(p)
                for tok in p.split():     # track latest full word, even within 'm. magistra'
                    if re.fullmatch(r"[A-Za-zāēīōūĀĒĪŌŪ]+", tok) and tok not in ("and", "or", "m", "f", "n"):
                        last = tok
        return ", ".join(out)
    return latin

def expand_first_conj(rtf_parts, quia_first):
    """Quia abbreviates regular 1st-conj verbs as 'cēnō (1)'. Rebuild the full
    macronized principal parts: the endings are fixed (-ō, -āre, -āvī, -ātum),
    the stem macrons come from Quia's first form."""
    stem = re.sub(r"ō$", "", quia_first)
    reg = [quia_first, stem + "āre", stem + "āvī", stem + "ātum"]
    out = []
    for i, p in enumerate(rtf_parts):
        out.append("-" if p == "-" else (reg[i] if i < 4 else p))
    return ", ".join(out) if out else ", ".join(reg)

# ---------------------------------------------------------------- macronize (rule fallback)
def expand_and_macronize(rec):
    """Best-effort macronization + abbreviation expansion when Quia has no entry.
    Deliberately conservative: macronize only the predictable endings; the result
    is ALWAYS flagged for review, so wrong guesses get caught by a human."""
    pos = rec["pos"]
    latin = rec["latin"]
    parts = [p.strip() for p in latin.split(",")]

    if pos.startswith("verb"):
        # Only macronize what's predictable from the conjugation; leave unpredictable
        # stem and perfect/supine vowels PLAIN (never guess a macron that could be
        # wrong — the entry is flagged for review either way).
        m = re.search(r"\((\d)i?\)", pos)
        conj = m.group(1) if m else None
        out = []
        for i, p in enumerate(parts):
            if p in ("-", ""):
                out.append(p); continue
            s = p
            if i == 0:                                   # 1sg present: -ō always long
                s = re.sub(r"o$", "ō", s)
            elif i == 1:                                 # infinitive: by conjugation
                if   conj == "1": s = re.sub(r"are$", "āre", s)
                elif conj == "2": s = re.sub(r"ere$", "ēre", s)
                elif conj == "4": s = re.sub(r"ire$", "īre", s)
                # 3 / 3i keep short -ere; irregular: leave as-is
            elif i == 2:                                 # perfect: 1sg -ī always long
                if   conj == "1": s = re.sub(r"avi$", "āvī", s)
                elif conj == "4": s = re.sub(r"ivi$", "īvī", s)
                s = re.sub(r"i$", "ī", s)
            elif i == 3:                                 # supine: only regular endings
                if   conj == "1": s = re.sub(r"atum$", "ātum", s)
                elif conj == "4": s = re.sub(r"itum$", "ītum", s)
            out.append(s)
        return ", ".join(out)

    if pos.startswith("noun"):
        nom = parts[0]
        # expand abbreviated genitive endings against the nominative
        if len(parts) >= 2 and parts[1].startswith("-"):
            ending = parts[1]
            gen = None
            if ending == "-ae" and nom.endswith("a"):
                gen = nom[:-1] + "ae"            # 1st decl: fama -> famae
            elif ending == "-i" and nom.endswith("us"):
                gen = nom[:-2] + "ī"             # 2nd decl: amicus -> amicī
            elif ending == "-i" and nom.endswith("um"):
                gen = nom[:-2] + "ī"
            elif ending == "-i" and nom.endswith("er"):
                gen = nom + "ī"                  # puer -> puerī (ager handled by review)
            parts[1] = gen if gen else ending
            return ", ".join(parts)
        return latin  # 3rd+ decl already give full genitive; leave for review

    # adjectives, pronouns, numerals, particles: no reliable rule -> leave as-is
    return latin

# ---------------------------------------------------------------- slot parsing
GENDER_RE = re.compile(r"^(m|f|n)\.?(\s*/\s*(m|f|n)\.?)*$", re.I)

def parse_slots(pos, latin):
    """Structured answer slots for the English->Latin input UI.
    Falls back to a single full-citation text slot for anything irregular."""
    # drop parenthetical annotations ('(indeclinable)', '(+ abl.)') from the
    # answer forms — they aren't part of what the player types
    parts = [re.sub(r"\s*\([^)]*\)", "", p).strip() for p in latin.split(",")]
    parts = [p for p in parts if p]

    if pos.startswith("verb"):
        # principal parts; '-' means 'this part does not exist'
        return {"kind": "verb",
                "parts": [{"text": "" if p == "-" else p, "exists": p != "-"}
                          for p in parts]}

    if pos.startswith("noun"):
        gender = None
        forms = []
        for p in parts:
            if GENDER_RE.match(p):
                gender = p.rstrip(".").lower()
            else:
                forms.append(p)
        # indeclinable nouns (nihil, n.) keep a gender box but have no genitive
        indecl = "indeclinable" in pos
        return {"kind": "noun",
                "nominative": forms[0] if forms else "",
                "genitive":   "" if indecl else (forms[1] if len(forms) > 1 else ""),
                "gender":     gender}

    if pos.startswith("adjective") and len(parts) > 1:
        return {"kind": "adjective", "forms": parts}

    # pronoun / numeral / indeclinable noun / particle / etc.
    return {"kind": "text", "forms": parts}

def extract_case(latin_plain, english):
    """Determine the case a preposition governs, from the latin annotation
    ('in (+ abl.)') or the English note ('(takes ablative)')."""
    pl = latin_plain.lower()
    has = lambda *ks: [k for k in ks if k in pl]
    cs = []
    if "+ acc" in pl or "+ acc." in pl: cs.append("acc")
    if "+ abl" in pl: cs.append("abl")
    if "+ gen" in pl: cs.append("gen")
    if "+ dat" in pl: cs.append("dat")
    if not cs:
        en = english.lower()
        if "accusative" in en: cs.append("acc")
        if "ablative" in en:  cs.append("abl")
        if "genitive" in en:  cs.append("gen")
        if "dative" in en:    cs.append("dat")
    return "/".join(cs)

# ---------------------------------------------------------------- english parsing
def parse_english(english):
    """Split the gloss into semicolon groups -> comma synonyms, pulling
    parenthetical idioms/notes aside so they don't pollute L->E grading."""
    notes = re.findall(r"\(([^)]*)\)", english)
    core = re.sub(r"\([^)]*\)", "", english)
    groups = []
    for grp in core.split(";"):
        syns = [s.strip().lower() for s in grp.split(",") if s.strip()]
        if syns:
            groups.append(syns)
    return {"raw": english, "groups": groups, "notes": [n.strip() for n in notes]}

# ---------------------------------------------------------------- main
def main():
    quia = parse_quia()
    recs = parse_rtf()
    overrides = load_overrides()
    splits = load_splits()

    MAJOR = {"noun", "adj", "verb"}
    cards, review = [], []
    matched = 0
    for idx, rec in enumerate(recs):
        cid = f"v{idx:04d}"
        key = head_key(rec["latin"])
        fam = pos_family(rec["pos"])
        rtf_parts = [p.strip() for p in rec["latin"].split(",")]
        cands = quia.get(key, [])
        # prefer a same-family Quia candidate; otherwise take the first available
        chosen = next(((t, f) for t, f in cands if f == fam), cands[0] if cands else None)
        review_reason = ""

        if not chosen:                                   # no Quia entry: rule-guess
            macron, source, needs_review = expand_and_macronize(rec), "rule", True
            review_reason = "no-quia-match"
        else:
            term, cfam = chosen
            quia_first = term.split(",")[0].split("(")[0].strip()
            if fam in MAJOR and cfam in MAJOR and cfam != fam:
                # genuine homograph (e.g. adj amīcus vs noun amīcus): keep RTF
                # structure but lift the stem macrons from Quia, then flag it
                first = transfer_macrons(rtf_parts[0], quia_first)
                macron = ", ".join([first] + rtf_parts[1:])
                source, needs_review = "homograph", True
                review_reason = f"homograph (quia is {cfam})"
            elif fam == "verb" and re.search(r"\(\d\)", term):
                macron, source, needs_review = expand_first_conj(rtf_parts, quia_first), "quia", False
                matched += 1
            else:
                macron, source, needs_review = term, "quia", False
                matched += 1

        # hand-filled corrections take precedence over any guess
        filled = cid in overrides
        if filled:
            macron, source, needs_review, review_reason = overrides[cid], "filled", False, ""

        # spell out all abbreviated forms (adj endings, compound-verb prefixes, noun genitives)
        macron = spell_out(rec["pos"], macron)

        special = bool(re.search(r"\((?:plural|gen\.|in plural|esp\.)", rec["english"], re.I) \
                       or re.search(r"\bplural\b", rec["english"], re.I))

        card = {
            "id": cid,
            "chapter": rec["chapter"],
            "pos": rec["pos"],
            "latin_plain": rec["latin"],
            "latin": macron,
            "english": parse_english(rec["english"]),
            "slots": parse_slots(rec["pos"], macron),
            "source": source,
            "needs_review": needs_review,
            "review_reason": review_reason,
            "filled": filled,
            "special_sense": special,
        }

        # prepositions: lift the governed case into a structured slot + dropdown,
        # and drop the "(takes …)" note from the English so it isn't shown as a hint
        if rec["pos"].startswith("preposition"):
            case = extract_case(rec["latin"], rec["english"])
            clean = re.sub(r"\s*\(takes[^)]*\)", "", rec["english"]).strip()
            forms = [re.sub(r"\s*\([^)]*\)", "", p).strip() for p in macron.split(",")]
            forms = [p for p in forms if p]
            card["english"] = parse_english(clean)
            card["slots"] = {"kind": "prep", "forms": forms, "case": case}

        cards.append(card)
        if needs_review:
            review.append(card)

    # --- split off idioms & distinct plural senses into their own cards ---
    byid = {c["id"]: c for c in cards}
    for pid, items in splits.items():
        parent = byid.get(pid)
        if not parent:
            continue
        raw = parent["english"]["raw"]
        raw = strip_last_paren(raw) if raw.rstrip().endswith(")") else strip_plural_sense(raw)
        parent["english"] = parse_english(raw)
    ordered = []
    for c in cards:
        ordered.append(c)
        for k, s in enumerate(splits.get(c["id"], [])):
            pos = "noun (plural)" if s["kind"] == "plural" else "idiom"
            ordered.append({
                "id": f"{c['id']}-s{k+1}",
                "chapter": c["chapter"], "pos": pos,
                "latin_plain": "", "latin": s["latin"],
                "english": parse_english(s["english"]),
                "slots": parse_slots(pos, s["latin"]),
                "source": "filled", "needs_review": False, "review_reason": "",
                "filled": True, "special_sense": False,
                "derived_from": c["id"], "split_kind": s["kind"],
            })
    cards = ordered

    payload = {"version": 1, "cards": cards}
    with open(os.path.join(ROOT, "cards.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)

    # cards.js sets a global so the app loads by double-click (file://) with no server
    with open(os.path.join(ROOT, "cards.js"), "w", encoding="utf-8") as f:
        f.write("window.WHEELOCK_CARDS = ")
        json.dump(payload, f, ensure_ascii=False)
        f.write(";\n")

    with open(os.path.join(ROOT, "macron_review.tsv"), "w", encoding="utf-8") as f:
        f.write("id\tchapter\tpos\treason\trtf_latin\tguess_macron\tcorrected_macron\tenglish\n")
        for c in review:
            f.write(f"{c['id']}\t{c['chapter']}\t{c['pos']}\t{c['review_reason']}\t"
                    f"{c['latin_plain']}\t{c['latin']}\t\t{c['english']['raw']}\n")

    n = len(cards)
    filled = sum(1 for c in cards if c["filled"])
    need = sum(1 for c in cards if c["needs_review"])
    print(f"Total cards          : {n}  (incl. {sum(1 for c in cards if c.get('derived_from'))} split idiom/plural)")
    print(f"Macron-matched (Quia): {matched}")
    print(f"Hand-filled overrides: {filled}")
    print(f"Trusted total        : {n-need} ({100*(n-need)//n}%)")
    print(f"Still need review    : {need}")
    print(f"Special-sense flags  : {sum(1 for c in cards if c['special_sense'])}")
    print(f"Wrote cards.json, cards.js, macron_review.tsv")

if __name__ == "__main__":
    main()
