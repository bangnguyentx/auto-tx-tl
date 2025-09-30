CREATE TABLE users (
  id BIGINT PRIMARY KEY, -- telegram user id
  display_name TEXT,
  balance BIGINT DEFAULT 0,
  bonus_used BOOLEAN DEFAULT false,
  created_at TIMESTAMP DEFAULT now()
);

CREATE TABLE rounds (
  id BIGSERIAL PRIMARY KEY,
  started_at TIMESTAMP NOT NULL DEFAULT now(),
  rolled_at TIMESTAMP,
  d1 INT,
  d2 INT,
  d3 INT,
  result TEXT, -- 'TAI' or 'XIU'
  pot_snapshot BIGINT DEFAULT 0,
  overridden BOOLEAN DEFAULT false
);

CREATE TABLE bets (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT REFERENCES users(id),
  round_id BIGINT REFERENCES rounds(id),
  choice TEXT, -- 'TAI' or 'XIU'
  amount BIGINT,
  payout BIGINT DEFAULT 0,
  won BOOLEAN,
  created_at TIMESTAMP DEFAULT now()
);

CREATE TABLE stats (
  user_id BIGINT PRIMARY KEY,
  win_streak INT DEFAULT 0,
  max_win_streak INT DEFAULT 0,
  total_wins BIGINT DEFAULT 0,
  total_losses BIGINT DEFAULT 0
);

CREATE TABLE house (
  id INT PRIMARY KEY DEFAULT 1,
  balance BIGINT DEFAULT 0
);

CREATE TABLE withdraws (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT,
  amount BIGINT,
  status TEXT DEFAULT 'PENDING', -- PENDING, APPROVED, REJECTED
  created_at TIMESTAMP DEFAULT now(),
  handled_by BIGINT,
  handled_at TIMESTAMP
);

CREATE TABLE deposits (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT,
  amount BIGINT,
  status TEXT DEFAULT 'PENDING',
  created_at TIMESTAMP DEFAULT now(),
  handled_by BIGINT,
  handled_at TIMESTAMP
);
