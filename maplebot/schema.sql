PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS schema_versions(version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO schema_versions VALUES(1);
CREATE TABLE IF NOT EXISTS guild_settings(
 guild_id INTEGER PRIMARY KEY, manager_role_id INTEGER, default_channel_id INTEGER,
 last_channel_id INTEGER, active_generation INTEGER, preparing_generation INTEGER,
 generation_counter INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 0,
 initialized INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS discord_users(
 guild_id INTEGER NOT NULL REFERENCES guild_settings, user_id INTEGER NOT NULL,
 display_name TEXT NOT NULL, membership TEXT NOT NULL CHECK(membership IN ('present','left','unknown')),
 observed_at REAL NOT NULL, last_present_at REAL,
 PRIMARY KEY(guild_id,user_id)
);
CREATE TABLE IF NOT EXISTS jobs(
 id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL REFERENCES guild_settings,
 name TEXT NOT NULL, name_key TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
 version INTEGER NOT NULL DEFAULT 1, links_version INTEGER NOT NULL DEFAULT 0, icon_key TEXT,
 UNIQUE(guild_id,name_key), UNIQUE(guild_id,id)
);
CREATE TABLE IF NOT EXISTS cycles(
 id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL UNIQUE REFERENCES guild_settings,
 label TEXT NOT NULL DEFAULT '현재', UNIQUE(guild_id,id)
);
CREATE TABLE IF NOT EXISTS characters(
 id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, owner_id INTEGER NOT NULL,
 registered_by INTEGER NOT NULL, name TEXT NOT NULL, name_key TEXT NOT NULL,
 job_id INTEGER NOT NULL, hexa_value INTEGER CHECK(hexa_value IS NULL OR (hexa_value>0 AND hexa_value%100=0)),
 active INTEGER NOT NULL DEFAULT 1 CHECK(active IN(0,1)), version INTEGER NOT NULL DEFAULT 1,
 created_at REAL NOT NULL, updated_at REAL NOT NULL,
 UNIQUE(guild_id,name_key), UNIQUE(guild_id,id),
 FOREIGN KEY(guild_id,owner_id) REFERENCES discord_users(guild_id,user_id),
 FOREIGN KEY(guild_id,job_id) REFERENCES jobs(guild_id,id)
);
CREATE TABLE IF NOT EXISTS bosses(
 id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL REFERENCES guild_settings,
 name TEXT NOT NULL, name_key TEXT NOT NULL, difficulty TEXT NOT NULL, difficulty_key TEXT NOT NULL,
 display_name TEXT, max_members INTEGER NOT NULL CHECK(max_members BETWEEN 1 AND 6),
 sort_order INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'active' CHECK(status IN('active','inactive','deleted')),
 version INTEGER NOT NULL DEFAULT 1, composition_version INTEGER NOT NULL DEFAULT 0,
 UNIQUE(guild_id,name_key,difficulty_key), UNIQUE(guild_id,id)
);
CREATE TABLE IF NOT EXISTS boss_groups(
 id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, boss_id INTEGER NOT NULL,
 cycle_id INTEGER NOT NULL, name TEXT NOT NULL, sort_order INTEGER NOT NULL DEFAULT 0,
 status TEXT NOT NULL DEFAULT 'active' CHECK(status IN('active','deleted')), version INTEGER NOT NULL DEFAULT 1,
 UNIQUE(guild_id,boss_id,cycle_id,id), UNIQUE(guild_id,id),
 FOREIGN KEY(guild_id,boss_id) REFERENCES bosses(guild_id,id),
 FOREIGN KEY(guild_id,cycle_id) REFERENCES cycles(guild_id,id)
);
CREATE TABLE IF NOT EXISTS group_members(
 guild_id INTEGER NOT NULL, boss_id INTEGER NOT NULL, cycle_id INTEGER NOT NULL,
 group_id INTEGER NOT NULL, character_id INTEGER NOT NULL, joined_at REAL NOT NULL,
 PRIMARY KEY(guild_id,boss_id,cycle_id,character_id), UNIQUE(group_id,character_id),
 FOREIGN KEY(guild_id,boss_id,cycle_id,group_id) REFERENCES boss_groups(guild_id,boss_id,cycle_id,id),
 FOREIGN KEY(guild_id,character_id) REFERENCES characters(guild_id,id)
);
CREATE TABLE IF NOT EXISTS confirmations(
 id TEXT PRIMARY KEY, guild_id INTEGER NOT NULL, actor_id INTEGER NOT NULL, action TEXT NOT NULL,
 payload TEXT NOT NULL, revision INTEGER NOT NULL, expires_at REAL NOT NULL,
 consumed INTEGER NOT NULL DEFAULT 0, result TEXT
);
CREATE TABLE IF NOT EXISTS audit_logs(
 id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, actor_id INTEGER NOT NULL,
 action TEXT NOT NULL, payload TEXT NOT NULL, before_json TEXT NOT NULL, after_json TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS board_generations(
 guild_id INTEGER NOT NULL REFERENCES guild_settings, generation INTEGER NOT NULL,
 channel_id INTEGER NOT NULL, status TEXT NOT NULL CHECK(status IN('preparing','active','retired')),
 created_at REAL NOT NULL, last_error TEXT, PRIMARY KEY(guild_id,generation)
);
CREATE TABLE IF NOT EXISTS board_sync_state(
 guild_id INTEGER NOT NULL, generation INTEGER NOT NULL, boss_id INTEGER NOT NULL,
 requested INTEGER NOT NULL DEFAULT 0, completed INTEGER NOT NULL DEFAULT -1,
 retries INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0, last_error TEXT,
 PRIMARY KEY(guild_id,generation,boss_id),
 FOREIGN KEY(guild_id,generation) REFERENCES board_generations(guild_id,generation),
 FOREIGN KEY(guild_id,boss_id) REFERENCES bosses(guild_id,id)
);
CREATE TABLE IF NOT EXISTS board_messages(
 guild_id INTEGER NOT NULL, generation INTEGER NOT NULL, boss_id INTEGER NOT NULL,
 page INTEGER NOT NULL, channel_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
 render_version INTEGER NOT NULL, PRIMARY KEY(guild_id,generation,boss_id,page), UNIQUE(channel_id,message_id)
);
CREATE TABLE IF NOT EXISTS board_cleanup_messages(
 channel_id INTEGER NOT NULL, message_id INTEGER NOT NULL, guild_id INTEGER NOT NULL,
 generation INTEGER NOT NULL, last_error TEXT, retries INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
 PRIMARY KEY(channel_id,message_id)
);
CREATE TABLE IF NOT EXISTS board_jobs(
 id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, generation INTEGER NOT NULL,
 kind TEXT NOT NULL, state TEXT NOT NULL, last_error TEXT, created_at REAL NOT NULL,
 UNIQUE(guild_id,generation,kind)
);
CREATE INDEX IF NOT EXISTS members_character ON group_members(guild_id,character_id);
CREATE INDEX IF NOT EXISTS characters_owner ON characters(guild_id,owner_id);
