-- Trailmix Cloud. Times are milliseconds since 1970 (UTC). Months are 'YYYY-MM' in UTC.

CREATE TABLE accounts (
  id TEXT PRIMARY KEY,
  email TEXT NOT NULL UNIQUE,
  created_at INTEGER NOT NULL,
  trial_started_at INTEGER,                 -- null: this account never had a trial
  trial_seconds REAL NOT NULL DEFAULT 0     -- meeting seconds used in the trial (it's lifetime, not monthly)
);

-- A linked app. Only a hash of its token is kept; revoking it signs that Mac out.
CREATE TABLE devices (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL REFERENCES accounts(id),
  token_hash TEXT NOT NULL UNIQUE,
  name TEXT,
  device_hash TEXT,                          -- hash of the app's install id, for one trial per device
  created_at INTEGER NOT NULL,
  last_used_at INTEGER,
  revoked_at INTEGER
);
CREATE INDEX devices_account ON devices(account_id);

-- The paid plan, as the merchant of record last told us.
CREATE TABLE subscriptions (
  account_id TEXT PRIMARY KEY REFERENCES accounts(id),
  provider TEXT NOT NULL,                    -- lemonsqueezy
  provider_id TEXT NOT NULL,
  customer_id TEXT,
  plan TEXT NOT NULL,                        -- basic | pro | max
  status TEXT NOT NULL,                      -- the provider's word: active, on_trial, past_due, cancelled, paused, unpaid, expired
  period_end INTEGER,
  portal_url TEXT,
  provider_updated_at INTEGER,               -- the provider's own timestamp, so an older event arriving late is ignored
  updated_at INTEGER NOT NULL
);

CREATE TABLE usage_month (
  account_id TEXT NOT NULL,
  month TEXT NOT NULL,
  pro_seconds REAL NOT NULL DEFAULT 0,       -- meeting seconds at the Pro level (trial included)
  basic_seconds REAL NOT NULL DEFAULT 0,
  cost_micros INTEGER NOT NULL DEFAULT 0,    -- what Google charged us, in millionths of a dollar
  requests INTEGER NOT NULL DEFAULT 0,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (account_id, month)
);

-- Meeting hours: the audio seconds of each stream of a meeting (role + track), and how many were counted. A meeting
-- counts as long as its longest stream, so the mic and call tracks, or captions plus a later pass, aren't added up.
CREATE TABLE meeting_streams (
  account_id TEXT NOT NULL,
  month TEXT NOT NULL,
  meeting_id TEXT NOT NULL,
  stream TEXT NOT NULL,
  seconds REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (account_id, month, meeting_id, stream)
);
CREATE TABLE meetings (
  account_id TEXT NOT NULL,
  month TEXT NOT NULL,
  meeting_id TEXT NOT NULL,
  counted_seconds REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (account_id, month, meeting_id)
);

CREATE TABLE trial_devices (
  device_hash TEXT PRIMARY KEY,
  account_id TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE TABLE sessions (
  token_hash TEXT PRIMARY KEY,
  account_id TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
);

CREATE TABLE magic_links (
  token_hash TEXT PRIMARY KEY,
  email TEXT NOT NULL,
  next TEXT,
  expires_at INTEGER NOT NULL,
  used_at INTEGER
);

-- Linking the app: the person sees `code`; the app polls with a secret whose hash is `poll_hash`.
CREATE TABLE link_codes (
  code TEXT PRIMARY KEY,
  poll_hash TEXT NOT NULL UNIQUE,
  device_name TEXT,
  device_hash TEXT,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  account_id TEXT,                           -- set once someone approves it
  device_token TEXT,                         -- held only until the app collects it
  delivered_at INTEGER
);

CREATE TABLE passkeys (
  id TEXT PRIMARY KEY,                       -- credential id, base64url
  account_id TEXT NOT NULL,
  public_key BLOB NOT NULL,
  counter INTEGER NOT NULL DEFAULT 0,
  transports TEXT,
  created_at INTEGER NOT NULL,
  last_used_at INTEGER
);
CREATE INDEX passkeys_account ON passkeys(account_id);

CREATE TABLE challenges (
  id TEXT PRIMARY KEY,
  challenge TEXT NOT NULL,
  account_id TEXT,
  expires_at INTEGER NOT NULL
);

-- Whose uploads, files and batch jobs at Google are whose, so one account can't reach another's.
CREATE TABLE uploads (
  upload_id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL,
  upstream_url TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE TABLE files (
  name TEXT PRIMARY KEY,                     -- files/abc123
  account_id TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE TABLE batches (
  name TEXT PRIMARY KEY,                     -- batches/abc123
  account_id TEXT NOT NULL,
  role TEXT NOT NULL,
  plan TEXT NOT NULL,
  level TEXT NOT NULL,
  model TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  metered_at INTEGER
);

CREATE TABLE rate_limits (
  key TEXT NOT NULL,
  window INTEGER NOT NULL,
  count INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (key, window)
);
