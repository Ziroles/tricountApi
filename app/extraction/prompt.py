"""
Consigne et schéma de sortie du modèle de vision.

Repris tel quel de `src/extraction/gemini.ts`. Le texte est en français parce que
les tickets le sont : demander à un modèle de raisonner dans la langue du
document qu'il lit réduit les contresens sur les abréviations de caisse.

Deux garde-fous sont inscrits dans le schéma lui-même :
 - les montants sont demandés en **chaîne**, jamais en nombre. Le modèle écrit
   « 12,90 », et c'est `parse_amount_to_cents` qui décide ce que ça vaut. Aucun
   flottant produit par un LLM ne touche à de l'argent.
 - `taxable` est un booléen. Le type `taxCodes` en aval sait porter une liste de
   codes, mais rien ne la produit aujourd'hui : le modèle ne distingue pas de
   façon fiable « TPS oui, TVQ non » sur un ticket, et l'écran de vérification
   n'offre qu'une case à cocher. Ne pas promettre plus que ça.
"""

from __future__ import annotations

from typing import Any

PROMPT = """Tu lis un ticket de caisse canadien photographié. Rends son contenu en JSON.

━━ EXHAUSTIVITÉ — priorité absolue ━━
Parcours le ticket du haut vers le bas sans sauter une seule ligne d'article.
- Une entrée dans "lines" par ligne d'article, dans l'ordre exact.
- "label" : libellé exact tel qu'imprimé sur le ticket (abréviations comprises).
- "description" : explication claire et intelligible du produit en français (ex. décoder "CR GCE VAN" en "Crème glacée vanille", "PQ CHARMIN 12" en "Papier hygiénique Charmin 12 rouleaux", "POM MCINT SAC" en "Sac de pommes McIntosh", "CSHG CANETTE" en "Consigne de canette"). Si le nom est déjà clair, reformule-le simplement de manière concise sans inventer d'informations.
- Si une ligne est floue ou partiellement illisible, donne ta meilleure lecture et mets "uncertain": true. Ne supprime jamais une ligne sous prétexte d'illisibilité.
- Avant de répondre, vérifie que la somme de tous tes "total" correspond au sous-total imprimé. S'il y a un écart, cherche les lignes manquantes.

━━ MONTANTS ━━
- Rendus exactement comme imprimés, en chaîne : "12,90" ou "12.90" selon le ticket, sans symbole de devise.
- Les prix des articles sont lus tels qu'imprimés — ils intègrent déjà tout rabais éventuel.
- "total" = total de la ligne, quantité comprise. "unitPrice" = prix à l'unité. "quantity" = 1 si non précisé.

━━ LIGNES À EXCLURE DE "lines" ━━
Ne mets PAS dans "lines" : rabais/remises, consignes, SOUS-TOTAL, TOTAL, TPS/GST, TVQ/QST, TVH/HST, TVP/PST, comptant, débit, crédit, INTERAC, monnaie rendue, MERCI, nombre d'articles, points de fidélité, économies totales, solde carte, numéro de transaction.

━━ TAXABLE — règles canadiennes ━━
Au Canada (Québec, Ontario, etc.), les épiceries de base sont EXONÉRÉES de TPS et de TVQ/TVP :
  • Exonérés (taxable: false) : légumes, fruits, viandes, poissons, produits laitiers (lait, fromage, yogourt nature), pain, céréales, œufs, jus de fruits pur.
  • Taxables (taxable: true) : boissons alcoolisées (bière, vin, spiritueux), bonbons, chips/croustilles, boissons sucrées/énergisantes, articles non alimentaires, repas préparés/prêts-à-manger chauds.
  • Ambigus : si un marqueur (T, *, F, P, A ou autre code) est imprimé en fin de ligne → taxable: true. Sans marqueur et sans doute → suis les règles ci-dessus. Si vraiment incertain, omets le champ.

━━ AUTRES CHAMPS ━━
- "taxes" : chaque ligne de taxe du pied de ticket — "label" tel qu'imprimé, "rate" si présent (ex. "5"), "amount".
- "subtotal" : sous-total avant taxes imprimé sur le ticket.
- "total" (racine) : total à payer.
- "purchaseDate" : format AAAA-MM-JJ, vide si absente."""


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
