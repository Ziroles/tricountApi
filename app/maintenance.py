"""
Entretien : purge des photos de tickets.

Sur un téléphone, c'était le quota du navigateur qui faisait le ménage. Sur un
serveur, personne : sans purge, les photos s'accumulent indéfiniment, pour tous
les groupes, jusqu'à remplir le volume.

Ce qui disparaît, c'est **la photo, pas le ticket**. Les lignes, les taxes et la
répartition ont été vérifiées par un humain à l'écran de vérification ; la photo
n'est qu'une pièce justificative, utile quelques semaines. On efface donc le
fichier et on remet `image_id` à NULL, pour que l'interface sache qu'il n'y a
plus de photo au lieu d'en réclamer une introuvable.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from . import config, db

logger = logging.getLogger("splitticket.maintenance")


def purge_old_images(days: int | None = None) -> int:
    """
    Efface les photos plus vieilles que la rétention. Renvoie le nombre effacé.

    Une rétention nulle ou négative désactive la purge : c'est un choix explicite
    de l'hébergeur, pas une valeur à corriger en silence.
    """
    retention = config.IMAGE_RETENTION_DAYS if days is None else days
    if retention <= 0:
        return 0

    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention)).isoformat(timespec="seconds")
    rows = db.query("SELECT id, path, receipt_id FROM image WHERE created_at < ?", (cutoff,))
    if not rows:
        return 0

    removed = 0
    for row in rows:
        # Le fichier d'abord : si la base survit à un plantage entre les deux, il
        # reste une référence sans fichier, que la route image traite déjà en 404.
        # L'inverse — une ligne effacée et un fichier orphelin — ne se rattrape pas.
        (config.IMAGES_DIR / row["path"]).unlink(missing_ok=True)
        with db.transaction() as connection:
            connection.execute("DELETE FROM image WHERE id = ?", (row["id"],))
            connection.execute(
                "UPDATE receipt SET image_id = NULL WHERE id = ? AND image_id = ?",
                (row["receipt_id"], row["id"]),
            )
        removed += 1

    logger.info("purge : %d photo(s) de plus de %d jours effacée(s)", removed, retention)
    return removed


def purge_orphan_files() -> int:
    """
    Efface les fichiers du disque qu'aucune ligne ne référence.

    Filet pour les interruptions : un envoi coupé entre l'écriture du fichier et
    l'insertion en base laisse un fichier que plus rien ne nomme.
    """
    known = {row["path"] for row in db.query("SELECT path FROM image")}
    removed = 0
    for path in config.IMAGES_DIR.glob("*.bin"):
        if path.name not in known:
            path.unlink(missing_ok=True)
            removed += 1
    if removed:
        logger.info("purge : %d fichier(s) orphelin(s) effacé(s)", removed)
    return removed


async def run_periodically() -> None:
    """
    Boucle d'entretien, lancée au démarrage.

    Une première passe immédiate — un serveur redémarré souvent doit quand même
    faire le ménage — puis une par jour. Les erreurs sont journalisées et
    n'arrêtent pas la boucle : l'entretien ne doit jamais tuer le service.
    """
    while True:
        try:
            purge_old_images()
            purge_orphan_files()
        except Exception:  # noqa: BLE001 — l'entretien ne fait pas tomber le service
            logger.exception("la purge a échoué ; nouvelle tentative au prochain cycle")
        await asyncio.sleep(config.PURGE_INTERVAL_SECONDS)
