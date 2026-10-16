import { createHmac } from "node:crypto";
import { config } from "./config.js";

function publicClan(clan) {
  return {
    id: clan.id,
    name: clan.name,
    tag: clan.tag,
    leader_name: clan.leaderName,
    declared_members: clan.declaredMembers,
    server_members: clan.serverMembers ?? 0,
    discord_url: clan.discordUrl,
    logo_source_url: clan.logoUrl || null,
    logo_sha256: clan.logoHash || null,
    logo_mime: clan.logoMime || null,
    description: clan.description,
    goals: clan.goals,
    recruitment_status: clan.recruitmentStatus === "open" ? "open" : "closed",
    recruitment_url: clan.recruitmentUrl || null,
    created_at: clan.createdAt,
    updated_at: clan.updatedAt,
  };
}

export async function syncSite(clans, logger = console) {
  if (!config.syncUrl) return { skipped: true };
  const body = JSON.stringify({
    version: 1,
    generated_at: new Date().toISOString(),
    source: { guild_id: config.guildId },
    clans: clans.map(publicClan).slice(0, 100),
  });
  const timestamp = String(Math.floor(Date.now() / 1000));
  const signature = createHmac("sha256", config.syncSecret).update(`${timestamp}.${body}`).digest("hex");
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15_000);
  try {
    const response = await fetch(config.syncUrl, {
      method: "POST",
      body,
      redirect: "error",
      signal: controller.signal,
      headers: {
        "content-type": "application/json",
        "user-agent": "WARDOGS-RU-Clans-Bot/1.0",
        "x-wardogs-timestamp": timestamp,
        "x-wardogs-signature": signature,
      },
    });
    const result = await response.text();
    if (!response.ok) throw new Error(`Site sync failed (${response.status}): ${result.slice(0, 200)}`);
    return { skipped: false, status: response.status };
  } catch (error) {
    logger.error("Site synchronization failed", error);
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}
