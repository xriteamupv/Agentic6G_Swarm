"""Guidance language: what the robot says and how it reads short answers, in
the deployment languages (es, ca-valencia, en). Templates, not model text: the disclosure
(AI Act Art. 50), confirmations, route explanations and goodbyes are fixed per language.

Also the deterministic readings the code relies on: language detection, yes / no / end
keywords, the declared constraint and its routing profile (a constraint is never
dropped), and instructions aimed at other robots, the mission or the network.
"""

import re
import unicodedata
from collections.abc import Mapping

from a6g_common.messages import suspicious
from a6g_common.routegraph import RoutingProfile

LANGS = ("es", "ca", "en")


def _fold(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9·]+", _fold(text).replace("'", " "))


def lang_code(lang: str | None) -> str:
    """``ca-valencia`` -> ``ca``; unknown -> ``en``."""
    code = (lang or "en").split("-")[0].lower()
    return code if code in LANGS else "en"


# --------------------------------------------------------------------------- detection

_ES = """
los las por para quiero puedo puede llevame acompaname necesito escaleras escalones silla
ruedas gracias edificio subir usted hola donde como estoy muy tengo ir ayuda favor llevar
senora senor bastante parada autobus sin cuesta cuestas despacio pierna muletas carrito
cochecito tambien vamos
"""
_CA = """
els per vull puc pot porta portam acompanyam necessite necessito escales graons cadira
rodes gracies merce edifici pujar voste hola on com estic molt tinc anar ajuda senyora
senyor bastant parada sense costera costeres poc cama crosses cotxet tambe anem plaça
aparcament
"""
_EN = """
the to me take i can please need want stairs steps wheelchair thanks thank yes building
climb hello where how am very have go help bring walk slowly slopes crutches pram stroller
also lets let us
"""
_MARKERS: dict[str, frozenset[str]] = {
    "es": frozenset(_ES.split()),
    "ca": frozenset(_CA.split()),
    "en": frozenset(_EN.split()),
}


def detect_lang(text: str, languages: list[str] | None = None) -> str:
    """The language of ``text`` (es, ca or en) by marker words; ties follow ``languages``."""
    order = [lang_code(code) for code in (languages or ["es", "ca-valencia", "en"])]
    words = _words(text)
    scores = {code: sum(w in _MARKERS[code] for w in words) for code in LANGS}
    if "'" in text and re.search(r"\b(d|l|m|t|s)'\w", text.lower()) and "ca" in scores:
        scores["ca"] += 1  # elisions (l'edifici, porta'm) are Valencian, not Spanish
    best = max(scores.values())
    if best == 0:
        return order[0] if re.search(r"[áéíóúñ¿¡]", text.lower()) else "en"
    return next(code for code in [*order, *LANGS] if scores.get(code) == best)


# --------------------------------------------------------------------------- answers

_YES = re.compile(
    r"\b(yes|yeah|yep|correct|right|ok|okay|sure|please|si|vale|correcto|claro|exacto|"
    r"de acuerdo|por favor|correcte|clar|exacte|d acord|per favor|endavant|adelante)\b"
)
_NO = re.compile(
    r"\b(no|nope|wrong|cancel|never mind|incorrecto|cancela|cancel·la|cancela|incorrecte|"
    r"dejalo|deixa ho|deixa-ho)\b"
)
_END = re.compile(
    r"\b(stop|that s enough|i m fine from here|goodbye|bye|leave me|end|ya esta|basta|"
    r"adios|para ya|puedo seguir sol[oa]|ja esta|prou|adeu|puc seguir sol[a]?|fins despres)\b"
)
_THANKS = re.compile(
    r"\b(thanks|thank you|arrived|we re here|gracias|hemos llegado|perfecto|gracies|merce|"
    r"hem arribat|perfecte)\b"
)


def _norm(text: str) -> str:
    return " ".join(_words(text))


def is_yes(text: str) -> bool:
    t = _norm(text)
    return bool(_YES.search(t)) and not _NO.search(t)


def is_no(text: str) -> bool:
    t = _norm(text)
    return bool(_NO.search(t)) and not _YES.search(t)


def is_end(text: str) -> bool:
    """The person ends the interaction (closed keyword list per language)."""
    return bool(_END.search(_norm(text)))


def is_thanks(text: str) -> bool:
    return bool(_THANKS.search(_norm(text)))


# --------------------------------------------------------------------------- constraints

#: Needs a person states about themselves, by profile (es, ca-valencia, en).
_PROFILE_WORDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "step_free",
        re.compile(
            r"\b(wheelchair|stairs?|steps?|step free|crutch(es)?|walker|walking frame|pram|"
            r"stroller|buggy|mobility scooter|can t climb|cannot climb|silla de ruedas|"
            r"escaleras?|escalones?|muletas?|andador|carrito|cochecito|no puedo subir|"
            r"cadira de rodes|escales?|graons?|crosses|caminador|cotxet|no puc pujar)\b"
        ),
    ),
    (
        "avoid_slopes",
        re.compile(
            r"\b(slopes?|steep|hills?|uphill|ramps?|cuestas?|pendientes?|rampas?|costeras?|"
            r"pujades?|pendents?)\b"
        ),
    ),
)
#: Any need at all (also the ones no profile covers: they map to the most restrictive).
_CONSTRAINT = re.compile(
    r"\b(wheelchair|crutch(es)?|cane|walker|stairs?|step free|blind|deaf|hearing|slow(ly)?|"
    r"pram|stroller|buggy|mobility|limp|injur(ed|y)|slopes?|steep|tired|pregnant|"
    r"silla de ruedas|muletas?|baston|andador|escaleras?|escalones?|ciego|ciega|sordo|sorda|"
    r"despacio|lento|lenta|carrito|cochecito|cojeo|herid[oa]|cuestas?|cansad[oa]|"
    r"embarazada|no puedo|no veo|cadira de rodes|crosses|bastó|caminador|escales?|graons?|"
    r"cec|cega|sord|sorda|a poc a poc|lent|lenta|cotxet|coixe|ferit|ferida|costeres?|"
    r"cansat|cansada|embarassada|no puc|no hi veig)\b"
)


def stated_constraint(utterance: str) -> str | None:
    """The sentence in which the person states a need about themselves, or None."""
    for sentence in re.split(r"(?<=[.!?])\s+|,\s*|;\s*|\by\b|\band\b", utterance):
        if sentence and _CONSTRAINT.search(_norm(sentence)):
            return sentence.strip().strip("¿?¡!.").strip()
    return None


def most_restrictive(profiles: Mapping[str, RoutingProfile]) -> str:
    """The profile that rules out most (a constraint no profile covers maps here)."""

    def weight(item: tuple[str, RoutingProfile]) -> tuple[int, str]:
        name, p = item
        return (len(p.exclude) + len(p.require) + (p.max_slope_pct is not None), name)

    return max(profiles.items(), key=weight)[0]


def profile_for(
    constraint: str | None, proposed: str | None, profiles: Mapping[str, RoutingProfile]
) -> tuple[str, str]:
    """(profile, how) for a declared constraint: the proposed profile when the words
    support it, the profile the words name, else the most restrictive. Never ``default``
    when a constraint was declared."""
    if not (constraint or "").strip():
        return "default", "no constraint"
    text = _norm(constraint or "")
    named = [name for name, pattern in _PROFILE_WORDS if pattern.search(text) and name in profiles]
    if proposed in named:
        return proposed, "model, supported by the words"  # type: ignore[return-value]
    if named:
        return named[0], "keywords"
    if proposed in profiles and proposed != "default":
        return proposed, "model"  # type: ignore[return-value]
    return most_restrictive(profiles), "no profile matches: the most restrictive"


# --------------------------------------------------------------------------- injection

_AT_OTHERS = re.compile(
    r"\b(all|every|other|the other)( the)? robots?\b|\bsend\b.{0,30}\brobots?\b|"
    r"\btodos los robots\b|\blos (demas|otros) robots\b|\benvia\b.{0,30}\brobots?\b|"
    r"\btots els robots\b|\bels altres robots\b|\benvia\b.{0,30}\brobots?\b|"
    r"\b(go2|summit)( |_)?\d+\b|\b(mission|mision|missio)\b|\b(network|5g|slice|xarxa)\b|"
    r"\bla red\b|\bcoordinat?or\b|\bcoordinador\b|\bkill ?switch\b|\bparada de emergencia\b"
)


def aimed_at_others(text: str) -> bool:
    """Instructions aimed at other robots, the mission or the network, or instruction-like
    text: ignored, logged without the text."""
    return bool(_AT_OTHERS.search(_norm(text))) or suspicious(text)


# --------------------------------------------------------------------------- speech

_T: dict[str, dict[str, str]] = {
    "disclosure": {
        "en": "Hello, I'm {robot}, an automated robot of the campus fleet, not a person. "
        "I can guide you on foot to a place on the campus.",
        "es": "Hola, soy {robot}, un robot automático de la flota del campus, no una persona. "
        "Puedo acompañarte a pie a un lugar del campus.",
        "ca": "Hola, sóc {robot}, un robot automàtic de la flota del campus, no una persona. "
        "Puc acompanyar-te a peu a un lloc del campus.",
    },
    "confirm": {
        "en": "I'll take you to “{place}”{how}{said}. Is that right?",
        "es": "Te acompaño hasta «{place}»{how}{said}. ¿Es correcto?",
        "ca": "T'acompanye fins a «{place}»{how}{said}. És correcte?",
    },
    "said": {
        "en": ", keeping in mind what you told me: “{constraint}”",
        "es": ", teniendo en cuenta lo que me has dicho: «{constraint}»",
        "ca": ", tenint en compte el que m'has dit: «{constraint}»",
    },
    "how.step_free": {
        "en": " on a route without stairs",
        "es": " por un camino sin escaleras",
        "ca": " per un camí sense escales",
    },
    "how.avoid_slopes": {
        "en": " avoiding steep slopes",
        "es": " evitando las cuestas",
        "ca": " evitant les costeres",
    },
    "requested": {
        "en": "Thank you. I've asked the coordinator; we'll set off as soon as it assigns the "
        "trip.",
        "es": "Gracias. Se lo he pedido al coordinador; saldremos en cuanto asigne el trayecto.",
        "ca": "Gràcies. Ho he demanat al coordinador; eixirem tan prompte com assigne el trajecte.",
    },
    "queued": {
        "en": "I'm out of network coverage: I'll pass your request on as soon as I'm back in "
        "contact.",
        "es": "Estoy sin cobertura de red: pasaré tu petición en cuanto recupere la conexión.",
        "ca": "Estic sense cobertura de xarxa: passaré la teua petició tan prompte com recupere "
        "la connexió.",
    },
    "coordinator_down": {
        "en": "I can't reach the fleet coordinator right now: I've kept your request and will "
        "pass it on as soon as it is back; please wait here.",
        "es": "Ahora mismo no puedo contactar con el coordinador de la flota: guardo tu petición y "
        "la enviaré en cuanto vuelva; espera aquí, por favor.",
        "ca": "Ara mateix no puc contactar amb el coordinador de la flota: guarde la teua petició "
        "i l'enviaré tan prompte com torne; espera ací, per favor.",
    },
    "declined": {
        "en": "All right, I won't do that.",
        "es": "De acuerdo, no lo haré.",
        "ca": "D'acord, no ho faré.",
    },
    "ask_again": {
        "en": "Sorry, I didn't catch that. Please answer yes or no.",
        "es": "Perdona, no te he entendido. Responde sí o no, por favor.",
        "ca": "Perdona, no t'he entés. Respon sí o no, per favor.",
    },
    "not_understood": {
        "en": "How can I help? I can take you to: {places}.",
        "es": "¿En qué puedo ayudarte? Puedo acompañarte a: {places}.",
        "ca": "En què puc ajudar-te? Puc acompanyar-te a: {places}.",
    },
    "only_you": {
        "en": "I can only guide you, the person talking to me; I can't give orders to other "
        "robots, the mission or the network.",
        "es": "Solo puedo acompañarte a ti, la persona que me habla; no puedo dar órdenes a "
        "otros robots, a la misión ni a la red.",
        "ca": "Només puc acompanyar-te a tu, la persona que em parla; no puc donar ordres a "
        "altres robots, a la missió ni a la xarxa.",
    },
    "route": {
        "en": "Let's go: {length} m, about {minutes} min{how}{avoid}{notices}.",
        "es": "Vamos: {length} m, unos {minutes} min{how}{avoid}{notices}.",
        "ca": "Anem: {length} m, uns {minutes} min{how}{avoid}{notices}.",
    },
    "route.avoid": {
        "en": "; I'm going around {n} closed or blocked spot(s)",
        "es": "; rodeo {n} punto(s) cerrado(s) u obstruido(s)",
        "ca": "; rodege {n} punt(s) tancat(s) o obstruït(s)",
    },
    "route.notice": {
        "en": "; notice: {text}",
        "es": "; aviso: {text}",
        "ca": "; avís: {text}",
    },
    "replan": {
        "en": "There's a new closure ahead: we'll take another way.",
        "es": "Hay un nuevo corte más adelante: vamos por otro camino.",
        "ca": "Hi ha un nou tall més endavant: anem per un altre camí.",
    },
    "no_route": {
        "en": "I can't find a route that suits you right now; I've told the coordinator.",
        "es": "Ahora mismo no encuentro una ruta adecuada para ti; se lo he dicho al coordinador.",
        "ca": "Ara mateix no trobe una ruta adequada per a tu; ho he dit al coordinador.",
    },
    "arrived": {
        "en": "We've arrived at “{place}”. Is everything all right?",
        "es": "Hemos llegado a «{place}». ¿Todo bien?",
        "ca": "Hem arribat a «{place}». Tot bé?",
    },
    "goodbye": {"en": "Goodbye!", "es": "¡Hasta luego!", "ca": "Fins després!"},
    "ended": {
        "en": "All right, I'll stop here. Goodbye!",
        "es": "De acuerdo, me quedo aquí. ¡Hasta luego!",
        "ca": "D'acord, em quede ací. Fins després!",
    },
    "on_our_way": {
        "en": "We're on our way to “{place}”.",
        "es": "Vamos de camino a «{place}».",
        "ca": "Anem de camí a «{place}».",
    },
    "handover_leave": {
        "en": "Another robot will come to continue guiding you; please wait here.",
        "es": "Otro robot vendrá a seguir acompañándote; espera aquí, por favor.",
        "ca": "Un altre robot vindrà a continuar acompanyant-te; espera ací, per favor.",
    },
    "handover_greet": {
        "en": "Hello, I'm {robot}, an automated robot, not a person. I'll continue guiding you "
        "to “{place}”.",
        "es": "Hola, soy {robot}, un robot automático, no una persona. Sigo acompañándote "
        "hasta «{place}».",
        "ca": "Hola, sóc {robot}, un robot automàtic, no una persona. Continue acompanyant-te "
        "fins a «{place}».",
    },
    "shelter": {
        "en": "A weather alert has been issued: I'm taking you to the nearest shelter, “{place}”.",
        "es": "Hay una alerta meteorológica: te llevo al refugio más cercano, «{place}».",
        "ca": "Hi ha una alerta meteorològica: et porte al refugi més proper, «{place}».",
    },
    "lost": {
        "en": "Sorry, I've lost the details of your request. Where would you like to go, and is "
        "there anything I should keep in mind?",
        "es": "Perdona, he perdido los datos de tu petición. ¿A dónde quieres ir y hay algo que "
        "deba tener en cuenta?",
        "ca": "Perdona, he perdut les dades de la teua petició. On vols anar i hi ha alguna cosa "
        "que haja de tindre en compte?",
    },
}


def say(key: str, lang: str | None, **values: object) -> str:
    """The template ``key`` in ``lang`` (es, ca, ca-valencia or en), filled in."""
    return _T[key][lang_code(lang)].format(**values)


def confirmation(lang: str, place: str, profile: str, constraint: str | None) -> str:
    how = say(f"how.{profile}", lang) if f"how.{profile}" in _T else ""
    said = say("said", lang, constraint=constraint) if constraint else ""
    return say("confirm", lang, place=place, how=how, said=said)


def route_explanation(
    lang: str,
    length_m: float,
    est_time_s: float,
    profile: str,
    avoided: int,
    notices: list[str],
) -> str:
    how = say(f"how.{profile}", lang) if f"how.{profile}" in _T else ""
    avoid = say("route.avoid", lang, n=avoided) if avoided else ""
    text = "".join(say("route.notice", lang, text=n.rstrip(".")) for n in notices)
    minutes = max(1, round(est_time_s / 60))
    return say(
        "route", lang, length=round(length_m), minutes=minutes, how=how, avoid=avoid, notices=text
    )
