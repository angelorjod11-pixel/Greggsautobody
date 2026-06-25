-- ============================================================
-- Polytrader database schema (PostgreSQL dialect) — generated from
-- polytrader/db/models.py (the canonical source). Do not hand-edit.
-- SQLite is created automatically; this file documents the DDL and is
-- the reference for a PostgreSQL deployment.
-- ============================================================

CREATE TABLE analysis_artifacts (
	id SERIAL NOT NULL, 
	as_of TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	kind VARCHAR(48) NOT NULL, 
	name VARCHAR(80) NOT NULL, 
	payload JSON NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_analysis_artifacts_as_of ON analysis_artifacts (as_of);
CREATE INDEX ix_artifact_kind_name ON analysis_artifacts (kind, name);
CREATE INDEX ix_analysis_artifacts_kind ON analysis_artifacts (kind);

CREATE TABLE markets (
	id VARCHAR(80) NOT NULL, 
	slug VARCHAR(256), 
	question TEXT, 
	category VARCHAR(40), 
	created_at TIMESTAMP WITHOUT TIME ZONE, 
	end_date TIMESTAMP WITHOUT TIME ZONE, 
	resolved BOOLEAN NOT NULL, 
	resolved_at TIMESTAMP WITHOUT TIME ZONE, 
	winning_outcome INTEGER, 
	volume_usd FLOAT NOT NULL, 
	liquidity_usd FLOAT NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_markets_category ON markets (category);

CREATE TABLE wallet_pairs (
	id SERIAL NOT NULL, 
	as_of TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	wallet_a VARCHAR(64) NOT NULL, 
	wallet_b VARCHAR(64) NOT NULL, 
	n_shared_markets INTEGER NOT NULL, 
	market_overlap FLOAT NOT NULL, 
	timing_jaccard FLOAT NOT NULL, 
	direction_corr FLOAT NOT NULL, 
	profit_corr FLOAT NOT NULL, 
	lead_lag_hours FLOAT NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_pair_ab ON wallet_pairs (wallet_a, wallet_b);
CREATE INDEX ix_wallet_pairs_as_of ON wallet_pairs (as_of);
CREATE INDEX ix_wallet_pairs_wallet_b ON wallet_pairs (wallet_b);
CREATE INDEX ix_wallet_pairs_wallet_a ON wallet_pairs (wallet_a);

CREATE TABLE wallets (
	address VARCHAR(64) NOT NULL, 
	label VARCHAR(128), 
	first_seen TIMESTAMP WITHOUT TIME ZONE, 
	last_seen TIMESTAMP WITHOUT TIME ZONE, 
	PRIMARY KEY (address)
);

CREATE TABLE positions (
	id SERIAL NOT NULL, 
	wallet_address VARCHAR(64) NOT NULL, 
	market_id VARCHAR(80) NOT NULL, 
	outcome_index INTEGER NOT NULL, 
	opened_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	closed_at TIMESTAMP WITHOUT TIME ZONE, 
	shares FLOAT NOT NULL, 
	avg_entry_price FLOAT NOT NULL, 
	cost_basis_usd FLOAT NOT NULL, 
	proceeds_usd FLOAT NOT NULL, 
	settlement_usd FLOAT NOT NULL, 
	realized_pnl_usd FLOAT NOT NULL, 
	roi FLOAT NOT NULL, 
	duration_hours FLOAT NOT NULL, 
	status VARCHAR(10) NOT NULL, 
	is_win BOOLEAN, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_position UNIQUE (wallet_address, market_id, outcome_index, opened_at), 
	FOREIGN KEY(wallet_address) REFERENCES wallets (address), 
	FOREIGN KEY(market_id) REFERENCES markets (id)
);
CREATE INDEX ix_positions_wallet_address ON positions (wallet_address);
CREATE INDEX ix_pos_wallet_status ON positions (wallet_address, status);
CREATE INDEX ix_positions_opened_at ON positions (opened_at);
CREATE INDEX ix_positions_market_id ON positions (market_id);

CREATE TABLE trades (
	id VARCHAR(120) NOT NULL, 
	wallet_address VARCHAR(64) NOT NULL, 
	market_id VARCHAR(80) NOT NULL, 
	outcome_index INTEGER NOT NULL, 
	side VARCHAR(4) NOT NULL, 
	timestamp TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	price FLOAT NOT NULL, 
	shares FLOAT NOT NULL, 
	usd_size FLOAT NOT NULL, 
	tx_hash VARCHAR(80), 
	PRIMARY KEY (id), 
	FOREIGN KEY(wallet_address) REFERENCES wallets (address), 
	FOREIGN KEY(market_id) REFERENCES markets (id)
);
CREATE INDEX ix_trades_wallet_market ON trades (wallet_address, market_id);
CREATE INDEX ix_trades_timestamp ON trades (timestamp);
CREATE INDEX ix_trades_market_ts ON trades (market_id, timestamp);
CREATE INDEX ix_trades_wallet_address ON trades (wallet_address);
CREATE INDEX ix_trades_market_id ON trades (market_id);

CREATE TABLE wallet_clusters (
	wallet_address VARCHAR(64) NOT NULL, 
	as_of TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	kmeans_cluster INTEGER NOT NULL, 
	hierarchical_cluster INTEGER NOT NULL, 
	pca_x FLOAT NOT NULL, 
	pca_y FLOAT NOT NULL, 
	community INTEGER NOT NULL, 
	PRIMARY KEY (wallet_address, as_of), 
	FOREIGN KEY(wallet_address) REFERENCES wallets (address)
);

CREATE TABLE wallet_metrics (
	wallet_address VARCHAR(64) NOT NULL, 
	as_of TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	n_positions INTEGER NOT NULL, 
	n_markets INTEGER NOT NULL, 
	active_days FLOAT NOT NULL, 
	trades_per_active_month FLOAT NOT NULL, 
	capital_deployed_usd FLOAT NOT NULL, 
	total_profit_usd FLOAT NOT NULL, 
	avg_roi FLOAT NOT NULL, 
	median_roi FLOAT NOT NULL, 
	profit_factor FLOAT NOT NULL, 
	win_rate FLOAT NOT NULL, 
	sharpe FLOAT NOT NULL, 
	max_drawdown FLOAT NOT NULL, 
	roi_stability FLOAT NOT NULL, 
	positive_month_rate FLOAT NOT NULL, 
	avg_position_size_usd FLOAT NOT NULL, 
	position_size_cv FLOAT NOT NULL, 
	median_hold_hours FLOAT NOT NULL, 
	entry_lead_days FLOAT NOT NULL, 
	markets_per_active_month FLOAT NOT NULL, 
	category_concentration FLOAT NOT NULL, 
	avg_entry_price FLOAT NOT NULL, 
	top_category VARCHAR(40), 
	eligible BOOLEAN NOT NULL, 
	extra JSON NOT NULL, 
	PRIMARY KEY (wallet_address, as_of), 
	FOREIGN KEY(wallet_address) REFERENCES wallets (address)
);
CREATE INDEX ix_wallet_metrics_eligible ON wallet_metrics (eligible);

CREATE TABLE wallet_scores (
	wallet_address VARCHAR(64) NOT NULL, 
	as_of TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	composite_score FLOAT NOT NULL, 
	rank INTEGER NOT NULL, 
	profitability_score FLOAT NOT NULL, 
	win_rate_score FLOAT NOT NULL, 
	consistency_score FLOAT NOT NULL, 
	risk_score FLOAT NOT NULL, 
	longevity_score FLOAT NOT NULL, 
	score_ci_low FLOAT NOT NULL, 
	score_ci_high FLOAT NOT NULL, 
	PRIMARY KEY (wallet_address, as_of), 
	FOREIGN KEY(wallet_address) REFERENCES wallets (address)
);
CREATE INDEX ix_wallet_scores_rank ON wallet_scores (rank);
CREATE INDEX ix_wallet_scores_composite_score ON wallet_scores (composite_score);
