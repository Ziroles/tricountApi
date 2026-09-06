# API SplitTicket

Le service qui porte [SplitTicket](https://github.com/MathieuMarthy/triCountHelper) : lecture
des tickets de caisse par IA de vision, groupes adossés à Tricount, stockage partagé des
tickets, et envoi de la dépense.

> [!WARNING]
> **Avertissement légal et technique**
> Tricount ne publie aucune interface programmable. Ce service utilise
> [`tricount-api`](https://github.com/elrandar/tricount-api), un client Python non officiel
> rétro-conçu depuis l'application Android.
> - Ce projet n'est ni affilié ni approuvé par Tricount ou bunq.
> - Cet usage sort des conditions d'utilisation du service.
> - Les points d'entrée peuvent changer ou disparaître sans préavis.

---

## Ce que fait le service

- **Groupes.** Un groupe *est* un tricount, identifié par son code d'invitation. Le rejoindre
  ramène la liste de ses membres : plus personne ne ressaisit les participants à la main.
- **Lecture des tickets.** L'appel à Gemini se fait ici, avec la clé de l'utilisateur ou celle
  de l'instance. Le résultat est **écrit sur le ticket avant de répondre** : une connexion qui
  tombe pendant la lecture ne fait plus perdre l'appel.
- **Tickets partagés.** Un ticket appartient au groupe, pas à l'appareil. Verrou optimiste par
  version : deux éditions simultanées ne s'écrasent pas en silence.
- **Identité par appareil.** Un appareil s'enrôle seul au premier lancement. Le compte est
  *optionnel*, et ne sert qu'à retrouver ses groupes ailleurs.
- **Envoi de la dépense.** Les parts portent les uuid des membres : plus d'appariement par nom.

Bâti sur FastAPI et SQLite. Les photos vont sur un volume, la base aussi.

---

## Prérequis

- **Recommandé** : [Docker](https://docs.docker.com/get-docker/) et Docker Compose
- **Sinon** : Python 3.12+ et `pip`

---

## Installation

### Docker Compose

1. **Cloner** :
   ```bash
   git clone https://github.com/Ziroles/tricountApi.git
   cd tricountApi
   cp .env.example .env
   ```

2. **Engendrer la clé de chiffrement** — elle protège les clés Gemini des utilisateurs au repos.
   Sans elle, le service refuse d'en conserver une plutôt que de l'écrire en clair :
   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
   Reportez-la dans `.env` sous `SPLITTICKET_SECRET_KEY`.

3. **Démarrer** :
   ```bash
   docker compose up -d
   docker compose logs -f
   ```
   Le service écoute sur `http://localhost:8787`. `GET /health` dit ce qu'il sait faire, et
   `/docs` expose la documentation OpenAPI engendrée.

### Sans Docker

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export SPLITTICKET_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
export SPLITTICKET_DATA_DIR=./data
uvicorn app.main:app --host 127.0.0.1 --port 8787
```

---

## Variables d'environnement

| Variable | Rôle | Défaut |
|---|---|---|
| `SPLITTICKET_SECRET_KEY` | **Requise** pour conserver les clés Gemini des utilisateurs (chiffrement au repos). | *aucun* |
| `SPLITTICKET_SIGNUP_KEY` | Optionnelle. Exigée pour enrôler un **nouvel** appareil (`X-Signup-Key`) : ferme une instance publique sans gérer de comptes. | vide (ouvert) |
| `GEMINI_API_KEY` | Optionnelle. Clé de repli quand l'utilisateur n'a pas la sienne. Vide = chacun apporte la sienne, et l'application le lui dit. | vide |
| `GEMINI_MODEL` | Modèle par défaut. | `gemini-2.5-flash` |
| `SPLITTICKET_DATA_DIR` | Base, photos, identifiants Tricount. | `/data` |
| `SPLITTICKET_ALLOWED_ORIGINS` | Origines autorisées, séparées par des virgules. | `*` |
| `SPLITTICKET_IMAGE_RETENTION_DAYS` | Purge des photos après N jours. `0` = jamais. | `90` |
| `SPLITTICKET_HOST` / `SPLITTICKET_PORT` | Écoute. | `127.0.0.1` / `8787` |
| `TRICOUNT_CREDENTIALS_PATH` | Identifiants d'appareil Tricount. | `$DATA_DIR/.tricount-credentials.json` |

`*` en origine autorisée convient à un déploiement personnel : l'authentification est un **jeton
porté**, pas un cookie, donc il n'y a pas de CSRF à craindre. Restreindre reste préférable en
public.

`SPLITTICKET_RELAY_KEY` n'existe plus ; `TRICOUNT_RELAY_KEY` est encore lue comme second recours
pour `SPLITTICKET_SIGNUP_KEY`, afin de ne pas casser un déploiement existant.

---

## Authentification

Aucune inscription n'est demandée à l'utilisateur. Au premier lancement, l'application appelle :

```http
POST /v1/devices
X-Signup-Key: <si l'instance en exige une>

→ 201 {"deviceId": "...", "token": "..."}
```

Le jeton est renvoyé **une seule fois** et présenté ensuite à chaque requête :

```http
Authorization: Bearer <token>
```

La base ne conserve que son empreinte : une copie du fichier ne suffit pas à se faire passer
pour un appareil.

Un **compte** (`POST /v1/accounts`, `POST /v1/sessions`) est optionnel. Il ne sert qu'à
rattacher plusieurs appareils aux mêmes groupes. Rattacher un appareil transfère ses accès au
compte, pour que rien ne disparaisse au passage.

---

## Routes

| | |
|---|---|
| `POST /v1/devices` | Enrôle un appareil |
| `POST /v1/accounts` · `POST /v1/sessions` | Compte optionnel |
| `GET /v1/me` · `PUT /v1/me/settings` | Clé Gemini, modèle |
| `GET /v1/models` | Modèles lisibles avec la clé effective |
| `GET · POST /v1/groups` | Lister, rejoindre par lien de partage |
| `GET · DELETE /v1/groups/{id}` | Consulter, quitter (pour soi seul) |
| `POST /v1/groups/{id}/members/refresh` | Resynchroniser les membres |
| `GET · POST /v1/groups/{id}/receipts` | Tickets du groupe |
| `GET · PUT · DELETE /v1/receipts/{id}` | Un ticket ; `PUT` porte sa `version` |
| `POST · GET /v1/receipts/{id}/image` | Photo |
| `POST /v1/receipts/{id}/scan` | Lecture OCR, écrite sur le ticket |
| `POST /v1/receipts/{id}/push` | Dépense dans le tricount |

Les erreurs portent un code exploitable : `{"detail": {"code": "no_gemini_key", "reason": "…"}}`.
`reason` est rédigé pour être affiché tel quel à l'utilisateur, en français.

### Concurrence

`PUT /v1/receipts/{id}` porte la `version` que le client croit modifier. Périmée, le serveur
répond `409` **en joignant le ticket courant** : le client peut expliquer ce qui s'est passé et
repartir, au lieu de recevoir un refus sec.

### Clé Gemini

L'ordre est : clé de l'utilisateur d'abord, clé de l'instance ensuite. Quelqu'un qui a pris la
peine d'enregistrer la sienne veut que ses lectures soient débitées chez lui. Si aucune n'est
disponible, la réponse est `400 {"code": "no_gemini_key"}`, que l'application traduit en
invitation à en renseigner une.

Les clés sont chiffrées au repos (Fernet) et ne ressortent **jamais** entières : l'API n'expose
qu'un indice, `AIza…7fQ`, assez pour que son propriétaire reconnaisse laquelle est en place.

---

## Entretien

Une tâche de fond tourne au démarrage puis une fois par jour : elle efface les photos plus
vieilles que `SPLITTICKET_IMAGE_RETENTION_DAYS`, ainsi que les fichiers qu'aucune ligne ne
référence (résidus d'un envoi interrompu).

**Seule la photo disparaît.** Les lignes du ticket ont été vérifiées par un humain à l'écran de
vérification ; la photo n'est qu'une pièce justificative, utile quelques semaines. Le ticket
reste, et son `imageId` repasse à `null` pour que l'application sache qu'il n'y a plus rien à
afficher plutôt que de réclamer un fichier absent.

Mettre `0` désactive la purge — c'est un choix, mais le volume grossit alors sans limite.

---

## Compatibilité des versions

`GET /health` annonce `contractVersion`. L'application la compare à la sienne au démarrage et
prévient l'utilisateur en cas d'écart, plutôt que d'échouer plus tard sur une route qui a changé
de forme. Incrémentez-la à chaque changement incompatible de la surface `/v1`.

---

## Portée et vie privée

Le service détient une **seule** identité d'appareil Tricount pour tous ses utilisateurs. Trois
conséquences assumées :

1. `list_tricounts()` renverrait les tricounts rejoints par *tous* les utilisateurs de
   l'instance. **Il n'est jamais appelé** : la liste des groupes vient de la table
   `group_access`, et d'elle seule. Un appareil qui demande un groupe auquel il n'a pas accès
   reçoit `404`, pas `403` — ne pas y avoir droit et ne pas exister doivent être indiscernables.
2. La lecture des membres passe par `get_tricount`, pas `join_tricount` : lire ne doit pas
   inscrire notre robot dans le tricount de quelqu'un.
3. Le quota est mutualisé. Si bunq coupe ce robot, l'instance entière tombe.

Hébergez donc pour vous et vos proches. Une instance ouverte au public devrait au minimum
définir `SPLITTICKET_SIGNUP_KEY`, et se passer de `GEMINI_API_KEY` pour que chacun paie ses
lectures.

---

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Tricount et Gemini sont simulés : la suite ne coûte rien et ne crée aucune vraie dépense.

`tests/test_normalize.py` et `tests/test_money.py` sont le **portage cas pour cas** des tests
TypeScript de la version précédente. Ce sont eux qui garantissent que déplacer l'OCR côté
serveur n'a pas déplacé un centime : tant qu'ils passent, la normalisation rend exactement les
mêmes montants que le client rendait.

---

## Structure

```
app/
  main.py            Application, CORS, démarrage
  config.py          Variables d'environnement
  db.py              SQLite, schéma, migrations par user_version
  auth.py            Appareils, comptes, propriétaire
  crypto.py          Chiffrement des clés Gemini au repos
  models.py          Contrat HTTP (Pydantic)
  tricount_client.py Accès à Tricount
  extraction/        money · normalize · prompt · gemini
  routes/            identity · groups · receipts
tests/
```

---

## Licence

Projet libre. Consultez les conditions d'utilisation de Tricount/bunq avant tout déploiement.
