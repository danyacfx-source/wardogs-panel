from app.discord import ALL_PERMS, compute_permissions

DISCORD_ID = "635370416252125184"
REF = f"user:{DISCORD_ID}"


def _binding(member=False, roles=None, discord_id=DISCORD_ID):
    return {
        "discord_id": discord_id,
        "guild_member": member,
        "roles": roles or [],
    }


def test_personal_ref_grants_full_access_without_membership():
    perms_map = {REF: sorted(ALL_PERMS)}
    assert compute_permissions(_binding(), {}, perms_map) == sorted(ALL_PERMS)


def test_personal_ref_unites_with_role_perms():
    perms_map = {"Moderator": ["kick"], REF: ["broadcast"]}
    binding = _binding(member=True, roles=["10"])
    assert compute_permissions(binding, {"10": "Moderator"}, perms_map) == ["broadcast", "kick"]


def test_personal_ref_skips_foreign_discord_id():
    perms_map = {REF: sorted(ALL_PERMS)}
    binding = _binding(discord_id="111")
    assert compute_permissions(binding, {}, perms_map) == []


def test_binding_without_discord_id_ignores_personal_refs():
    # staff-список строит binding без discord_id — чужой user-ref не должен учитываться
    binding = {"guild_member": True, "roles": ["10"]}
    perms_map = {"10": [], REF: sorted(ALL_PERMS)}
    assert compute_permissions(binding, {"10": "Nobody"}, perms_map) == []


def test_empty_personal_ref_keeps_roles_and_revokes_direct():
    perms_map = {REF: []}
    # член гильдии с ролью без прав — личный ref пуст, прав нет
    binding = _binding(member=True, roles=["10"])
    assert compute_permissions(binding, {"10": "Nobody"}, perms_map) == []
    # роль с правами продолжает работать при пустом личном ref
    perms_map = {REF: [], "Admin": sorted(ALL_PERMS)}
    binding = _binding(member=True, roles=["99"])
    assert compute_permissions(binding, {"99": "Admin"}, perms_map) == sorted(ALL_PERMS)


def test_personal_ref_filters_unknown_perms():
    perms_map = {REF: ["broadcast", "not_a_perm"]}
    assert compute_permissions(_binding(), {}, perms_map) == ["broadcast"]
