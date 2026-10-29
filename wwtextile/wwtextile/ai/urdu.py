"""Urdu / Roman Urdu understanding for the built-in rule engine.

This is a PHRASE TABLE, not a translator. It recognises the everyday ways a factory user
asks the questions the rule engine already supports and rewrites them into the English
form the router understands. Anything outside this vocabulary is not understood, and the
engine says so. Record numbers (lot, job, dispatch numbers) pass through untouched.
With an AI model connected, the model handles Urdu itself and this table is not used."""
from __future__ import annotations

import re

_URDU_SCRIPT = re.compile(r"[؀-ۿ]")

# Words that are Roman Urdu and not English; enough to flip the language detector.
_MARKERS = {
    "kitna", "kitni", "kitne", "kya", "hai", "hain", "nuqsan", "nuksan", "zaya", "dikhao", "dikhaiye", "batao", "bataiye", "maal", "paisa", "paise",
    "kaun", "kaunsa", "kaunsi", "kis", "aaj", "baqi", "baqaya", "haal", "kahan", "kyun", "kyon", "purana", "puran", "gaari", "gari", "kholo", "mujhe",
    "hamare", "humare", "hum", "chal", "raha", "rahi", "rahe", "sab", "wala", "wali", "lene", "dene", "der", "deri", "late", "khareed", "kharid", "bikri",
}

# (pattern, English). Longest / most specific first. Roman patterns are regexes on lower-case text.
_PHRASES: list[tuple[str, str]] = [
    # status of the whole factory
    (r"aaj (?:ka|ki) (?:haal|update|report|summary)|aaj kya (?:ho raha|chal raha|hua)|kya (?:ho raha|chal raha) hai|factory (?:ka )?(?:haal|update)|sab kuch (?:batao|dikhao)|تازہ صورتحال|آج کا حال|آج کیا ہو رہا ہے|کیا ہو رہا ہے", "what is happening in the factory today"),
    # money owed to us
    (r"kis (?:ke|ki|ka) (?:paise|paisa|rakam|payment) (?:baqi|baqaya|dena|dene|lene|lena)(?: hain| hai)?|kis se (?:paise|paisa) lene(?: hain| hai)?|kis ne (?:paise|paisa) dene(?: hain| hai)?|kon(?:sa)? customer (?:paise|paisa)|baqaya (?:rakam|paise)|وصول کرنے|کس سے پیسے لینے|کس نے پیسے دینے", "who owes us money receivables"),
    # money we owe
    (r"hum ne kis ko (?:paise|paisa) dene(?: hain| hai)?|hamen kis ko (?:paise|paisa) dene|kis ko (?:paise|paisa) dene|supplier ka (?:udhaar|baqaya)|ہمیں کس کو پیسے دینے|ہم نے کس کو پیسے دینے|ادا کرنے", "payables who do we owe"),
    (r"cash (?:kitna|kitni)|naqd|paisa kitna|paise kitne|nakad|کیش|نقد", "cash position"),
    # losses
    (r"kitna (?:nuqsan|nuksan|zaya|waste)|(?:nuqsan|nuksan|zaya|zayea|waste|kami) (?:kahan|kidhar|kaha)|nuqsan|nuksan|zaya|zayea|نقصان|ضائع|زیاں|کمی", "wastage and shrinkage losses"),
    (r"sikudna|sikur|sikurna|سکڑ", "shrinkage"),
    # overdue
    (r"kaun(?:sa|si|se)? (?:job|jobs)? ?(?:late|der|deri)|(?:late|der se|deri se) (?:job|jobs|kaam)|takheer|taakhir|تاخیر|لیٹ جاب|دیر سے", "overdue processing jobs"),
    # stock
    (r"purana (?:stock|maal|fabric)|puran[ae] (?:stock|maal)|sab se purana|پرانا اسٹاک|پرانا مال", "oldest stock ageing"),
    (r"(?:kitna|kitni|kitne) (?:stock|maal|kapra|fabric)|(?:stock|maal|kapra) (?:kitna|kitni|kitne)|stock ki (?:sorat|surat|halat)|اسٹاک کتنا|کتنا اسٹاک|کتنا مال|مال کتنا", "how much stock is on hand"),
    (r"kora kapra|kora kapda|کورا کپڑا", "kora fabric"),
    # multihead
    (r"kadhai|karhai|embroidery|multi ?head|machine(?:on| ka| ki)? (?:ka )?(?:haal|status)|مشین|کڑھائی|ملٹی ہیڈ", "multihead machines status"),
    # parties
    (r"(?:rang ?rez|dyeing|dye house|rangai|رنگ ?ریز)", "dyeing"),
    (r"chapai|chhapai|printing|چھپائی", "printing"),
    (r"top customers?|sab se (?:bara|barha|zyada) (?:customer|gahak)|bade customers?|بڑے کسٹمر", "top customers"),
    (r"(?:supplier|supplyer)(?:s)? se (?:kitna )?(?:khareed|kharid|maal)|khareed(?:ari)?|kharid(?:ari)?|خریداری", "purchases by supplier"),
    # gate / dispatch / alerts / reports
    (r"gaari|gari|gadi|vehicles?|گاڑی|گاڑیاں", "vehicles at gate"),
    (r"dispatch(?:es)?|bhejna|روانگی|ڈسپیچ", "dispatches"),
    (r"alerts?|khatr[ae]|warning|الرٹ|انتباہ", "alerts"),
    (r"receipts?|raseed|رسید", "goods receipts"),
    (r"reconciliation|hisab|حساب", "reconciliation exceptions"),
    # follow-ups
    (r"pehla (?:wala )?(?:kholo|dikhao|open)|پہلا کھولو", "open the first one"),
    (r"sab se purana (?:wala )?(?:kholo|dikhao)", "open the oldest one"),
    (r"(?:ye|yeh|yah) late kyun (?:hai|hua)|kyun late|دیر کیوں", "why is it delayed"),
    (r"agla kahan gaya|aage kahan gaya|kahan gaya|آگے کہاں گیا", "where did it go next"),
    # verbs / question words
    (r"dikhao|dikhaiye|dikhana|batao|bataiye|batana|دکھاؤ|دکھائیں|بتاؤ|بتائیں", "show"),
    (r"kholo|kholiye|کھولو", "open"),
    (r"kyun|kyon|کیوں", "why"),
    (r"kahan|kidhar|کہاں", "where"),
]
_FILLER = re.compile(r"\b(?:kya|hai|hain|ho|ka|ki|ke|ko|se|ne|mein|me|par|pe|aur|zara|please|plz|bhai|yaar|mujhe|hamen|humen|hamare|humare|abhi|wala|wali)\b|[؟،]")
_COMPILED = [(re.compile(p, re.I), e) for p, e in _PHRASES]

# short heading in the user's language for the answer's first tool; the detail stays in English
LEAD = {
    "roman": {"loss_analysis": "Nuqsan ki tafseel:", "party_balances": "Hisab kitab:", "cash_position": "Cash ki position:", "multihead_status": "Multihead ka haal:",
              "jobs_by_party": "Is party ke jobs:", "purchases_by_supplier": "Khareedari:", "top_customers": "Bade customers:", "stock_ageing": "Purana stock:",
              "factory_update": "Aaj ka haal:", "list_processing_jobs": "Jobs:", "search_inventory": "Stock ki position:", "gate_records": "Gate ka record:", "list_dispatches": "Dispatch:", "list_alerts": "Alerts:", "accessory_stock": "Accessories:", "finance_summary": "Finance:"},
    "script": {"loss_analysis": "نقصان کی تفصیل:", "party_balances": "حساب کتاب:", "cash_position": "کیش کی پوزیشن:", "multihead_status": "ملٹی ہیڈ کا حال:",
               "jobs_by_party": "اس پارٹی کے جابز:", "purchases_by_supplier": "خریداری:", "top_customers": "بڑے کسٹمر:", "stock_ageing": "پرانا اسٹاک:",
               "factory_update": "آج کا حال:", "list_processing_jobs": "جابز:", "search_inventory": "اسٹاک کی پوزیشن:", "gate_records": "گیٹ کا ریکارڈ:", "list_dispatches": "ڈسپیچ:", "list_alerts": "الرٹس:", "accessory_stock": "ایکسیسریز:", "finance_summary": "فنانس:"},
}
NOTICE = {"roman": "Tafseel abhi English mein hai; figures wohi hain jo records mein hain.",
          "script": "تفصیل ابھی انگریزی میں ہے؛ اعداد وہی ہیں جو ریکارڈ میں ہیں۔"}


def detect(text: str) -> str | None:
    """'script' for Urdu script, 'roman' for Roman Urdu, None for anything else."""
    if _URDU_SCRIPT.search(text or ""):
        return "script"
    words = set(re.findall(r"[a-z]+", (text or "").lower()))
    return "roman" if words & _MARKERS else None


def normalize(text: str) -> str:
    """Rewrite known Urdu/Roman Urdu phrases to English; keep record numbers and unknown words as they are."""
    t = " " + re.sub(r"\s+", " ", text.lower() if not _URDU_SCRIPT.search(text) else text).strip() + " "
    for rx, en in _COMPILED:
        t = rx.sub(f" {en} ", t)
    t = _FILLER.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()
