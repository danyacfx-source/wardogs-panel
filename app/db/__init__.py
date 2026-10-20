"""Пакет доступа к данным панели.

Раньше всё лежало в одном файле app/db.py (2125 строк). Модули ниже делят
логику по доменам. `from . import db` и `db.some_function()` продолжают
работать: __init__ ре-экспортирует всё публичное API.
"""

from .core import (
    REQUIRED_SCHEMA_VERSION,
    close,
    init,
    schema_version,
    log_site_audit,
    get_site_audit,
    verify_site_audit_chain,
    get_server_health,
    set_server_health,
    _conn,
    _now,
    _db_identity,
    _postgres_sql,
    _get_pg_pool,
    _PostgresDatabase,
    _PostgresConnection,
    _ConfiguredConnection,
    _PGCursor,
)

from .players import (
    upsert_user,
    touch_user,
    get_user,
    add_player_note,
    list_player_notes,
    delete_player_note,
    list_player_punishments,
    list_player_tags,
    add_player_tag,
    delete_player_tag,
)

from .clans import (
    create_clan,
    list_clans,
    get_clan,
    update_clan,
    update_clan_media,
    set_clan_member_role,
    remove_clan_member,
    delete_clan,
    set_player_clan,
    remove_player_clan,
    get_player_clan,
)

from .vip import (
    save_vip_slot,
    list_vip_slots,
    list_player_vip_slots,
    get_vip_slot,
    mark_vip_sync,
    delete_vip_slot,
    list_expired_vip_slots,
    create_vip_order,
    list_vip_orders,
    cancel_vip_order,
    get_vip_order,
    mark_vip_order,
)

from .tickets import (
    create_ticket,
    list_tickets,
    get_ticket,
    update_ticket,
    delete_ticket,
    add_ticket_comment,
)

from .bans import (
    save_managed_ban,
    list_managed_bans,
    list_expired_managed_bans,
    mark_managed_ban,
    delete_managed_ban,
)

from .auth import (
    list_staff_users,
    list_bindings,
    upsert_binding,
    get_binding,
    get_steam_by_discord,
    delete_binding,
    create_oauth_state,
    consume_oauth_state,
    claim_idempotency,
    complete_idempotency,
    abort_idempotency,
    new_session_token,
    create_session,
    get_session,
    delete_session,
    revoke_user_sessions,
    prune_sessions,
    get_role_perms,
    get_role_perms_all,
    set_role_perms,
)

from .killfeed import (
    record_kill_events,
    get_known_factions,
    recent_kills,
    killfeed_stats,
)
from .monitoring import (
    record_monitoring,
    get_population,
    get_player_activity,
    get_player_leaderboard,
    search_players,
    get_player_profile,
    get_player_presence,
    _aggregate_player_rows,
)

from .vip import _vip_row
from .tickets import _ticket_row
from .bans import _managed_ban_row
from .bots import _bot_row
from .auth import _session_key

from .bots import (
    create_bot_integration,
    get_bot_integration,
    list_bot_integrations,
    update_bot_integration,
    delete_bot_integration,
    append_bot_event,
    list_bot_events,
    save_public_registry,
    load_public_registry,
)
