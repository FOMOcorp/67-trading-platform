"""Поиск чатов, которые входят в папку (dialog filter) Telegram."""

from telethon import functions, utils
from telethon.tl.types import Channel, Chat, InputPeerSelf, User


def folder_title(dialog_filter) -> str | None:
    # в новых слоях API title — TextWithEntities, в старых — строка
    title = getattr(dialog_filter, "title", None)
    return getattr(title, "text", title)


def find_folder(filters, name: str):
    """Папка с названием name (без учёта регистра) или None."""
    wanted = name.strip().casefold()
    for f in filters:
        title = folder_title(f)
        if title is not None and title.strip().casefold() == wanted:
            return f
    return None


# что парсим: каналы и группы; личные чаты, боты и «Избранное» пропускаем
PARSED_KINDS = frozenset({"channel", "supergroup", "group"})


def chat_kind(entity) -> str:
    """Тип чата: channel, supergroup, group, bot, user, saved или other."""
    if isinstance(entity, Channel):
        return "channel" if entity.broadcast else "supergroup"
    if isinstance(entity, Chat):
        return "group"
    if isinstance(entity, User):
        if entity.is_self:
            return "saved"
        return "bot" if entity.bot else "user"
    return "other"


def peer_ids(peers) -> set[int]:
    # InputPeerSelf («Избранное») не приводится к id без клиента, его проверяем отдельно
    return {utils.get_peer_id(p) for p in peers if not isinstance(p, InputPeerSelf)}


def has_self(peers) -> bool:
    return any(isinstance(p, InputPeerSelf) for p in peers)


def matches_category(dialog_filter, entity, kind: str) -> bool:
    """Попадает ли чат в папку по флагам «Каналы», «Группы», «Боты», «Контакты», «Не контакты»."""
    if kind == "channel":
        flag = "broadcasts"
    elif kind in ("supergroup", "group"):
        flag = "groups"
    elif kind == "bot":
        flag = "bots"
    elif kind == "user":
        flag = "contacts" if entity.contact else "non_contacts"
    else:
        return False
    return bool(getattr(dialog_filter, flag, False))


def dialogs_in_folder(dialog_filter, dialogs) -> list:
    """Все диалоги из dialogs, которые попадают в папку, в исходном порядке.

    Учитываются явно добавленные и исключённые чаты, флаги типов чатов
    и «скрывать архив». exclude_read и exclude_muted не учитываются:
    они зависят от того, прочитан ли чат, и чат выпадал бы
    из парсинга после каждого прочтения.
    """
    include_peers = [*getattr(dialog_filter, "pinned_peers", []), *getattr(dialog_filter, "include_peers", [])]
    exclude_peers = getattr(dialog_filter, "exclude_peers", [])
    included, excluded = peer_ids(include_peers), peer_ids(exclude_peers)
    include_self, exclude_self = has_self(include_peers), has_self(exclude_peers)
    skip_archived = bool(getattr(dialog_filter, "exclude_archived", False))

    result = []
    for d in dialogs:
        kind = chat_kind(d.entity)
        is_self = kind == "saved"
        if d.id in excluded or (is_self and exclude_self):
            continue
        if d.id in included or (is_self and include_self):
            result.append(d)
        elif matches_category(dialog_filter, d.entity, kind) and not (skip_archived and d.archived):
            result.append(d)
    return result


def targets_in_folder(dialog_filter, dialogs) -> dict[int, object]:
    """Каналы и группы из папки, которые парсим: {peer_id: entity}."""
    return {
        d.id: d.entity
        for d in dialogs_in_folder(dialog_filter, dialogs)
        if chat_kind(d.entity) in PARSED_KINDS
    }


async def load_folder_dialogs(client, name: str) -> list:
    response = await client(functions.messages.GetDialogFiltersRequest())
    filters = getattr(response, "filters", response)
    dialog_filter = find_folder(filters, name)
    if dialog_filter is None:
        titles = [t for t in map(folder_title, filters) if t]
        raise LookupError(f"папка {name!r} не найдена, есть: {titles or 'ни одной'}")
    dialogs = await client.get_dialogs(archived=None)
    return dialogs_in_folder(dialog_filter, dialogs)


async def load_folder_targets(client, name: str) -> dict[int, object]:
    return {
        d.id: d.entity
        for d in await load_folder_dialogs(client, name)
        if chat_kind(d.entity) in PARSED_KINDS
    }
