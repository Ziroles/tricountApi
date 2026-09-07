"""
Instruction and output schema for the vision model.

Taken over from `src/extraction/gemini.ts`. The receipts themselves are often
printed in French (Quebec), so the French wording is kept alongside the English
one everywhere a literal token has to be recognised on the paper.

Two guardrails are written into the schema itself:
 - amounts are asked for as a **string**, never as a number. The model writes
   "12,90", and it is `parse_amount_to_cents` that decides what that is worth.
   No float produced by an LLM ever touches money.
 - `taxable` is a boolean. The `taxCodes` type downstream can carry a list of
   codes, but nothing produces one today: the model cannot reliably tell "GST
   yes, QST no" apart on a receipt, and the verification screen only offers a
   checkbox. Do not promise more than that.
"""

from __future__ import annotations

from typing import Any

PROMPT = """You are reading a photographed Canadian till receipt. Return its contents as JSON.

━━ COMPLETENESS — top priority ━━
Go through the receipt from top to bottom without skipping a single item line.
- One entry in "lines" per item line, in the exact order.
- "label": the label exactly as printed on the receipt (abbreviations included).
- "description": a clear, intelligible description of the product in English (e.g. decode "CR GCE VAN" as "Vanilla ice cream", "PQ CHARMIN 12" as "Charmin toilet paper, 12 rolls", "POM MCINT SAC" as "Bag of McIntosh apples", "CSHG CANETTE" as "Can deposit"). If the name is already clear, simply restate it concisely without inventing information.
- If a line is blurry or partly illegible, give your best reading and set "uncertain": true. Never drop a line on the grounds that it is illegible.
- Before answering, check that the sum of all your "total" values matches the printed subtotal. If there is a gap, look for the missing lines.

━━ AMOUNTS ━━
- Returned exactly as printed, as a string: "12,90" or "12.90" depending on the receipt, without a currency symbol.
- Item prices are read as printed — they already include any discount.
- "total" = the line total, quantity included. "unitPrice" = the price per unit. "quantity" = 1 if not stated.

━━ LINES TO EXCLUDE FROM "lines" ━━
Do NOT put in "lines": discounts/rebates (rabais, remise), container deposits (consigne), SUBTOTAL/SOUS-TOTAL, TOTAL, GST/TPS, QST/TVQ, HST/TVH, PST/TVP, cash/comptant, debit/débit, credit/crédit, INTERAC, change given/monnaie rendue, THANK YOU/MERCI, item count, loyalty points, total savings, card balance, transaction number.

━━ TAXABLE — Canadian rules ━━
In Canada (Quebec, Ontario, etc.), basic groceries are EXEMPT from GST and QST/PST:
  • Exempt (taxable: false): vegetables, fruit, meat, fish, dairy (milk, cheese, plain yogurt), bread, cereals, eggs, pure fruit juice.
  • Taxable (taxable: true): alcoholic drinks (beer, wine, spirits), candy, chips/crisps, sugary/energy drinks, non-food items, prepared/hot ready-to-eat meals.
  • Ambiguous: if a marker (T, *, F, P, A or another code) is printed at the end of the line → taxable: true. With no marker and no doubt → follow the rules above. If you are genuinely unsure, omit the field.

━━ OTHER FIELDS ━━
- "taxes": each tax line at the foot of the receipt — "label" as printed, "rate" if present (e.g. "5"), "amount".
- "subtotal": the pre-tax subtotal printed on the receipt.
- "total" (at the root): the amount due.
- "purchaseDate": YYYY-MM-DD format, empty if absent."""


RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "merchant": {"type": "string"},
        "purchaseDate": {"type": "string"},
        "subtotal": {"type": "string"},
        "total": {"type": "string"},
        "taxes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "rate": {"type": "string"},
                    "amount": {"type": "string"},
                },
                "required": ["label", "amount"],
            },
        },
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "description": {"type": "string"},
                    "quantity": {"type": "number"},
                    "unitPrice": {"type": "string"},
                    "total": {"type": "string"},
                    "taxable": {"type": "boolean"},
                    "uncertain": {"type": "boolean"},
                },
                "required": ["label", "total"],
            },
        },
    },
    "required": ["lines"],
}
