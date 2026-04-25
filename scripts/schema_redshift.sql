-- Redshift 版建表 DDL
-- 注意与 MySQL 版差异：
--   * TINYINT 不支持 → SMALLINT
--   * DATETIME → TIMESTAMP
--   * 不建索引（Redshift 用 SORTKEY/DISTKEY）
--   * 引号用双引号（此处全小写无保留字冲突，可省略）

DROP TABLE IF EXISTS ads_thor_fin_payment_iap_data_new;

CREATE TABLE ads_thor_fin_payment_iap_data_new (
  game_name           VARCHAR(64),
  uid                 VARCHAR(32),
  transaction_id      VARCHAR(64),
  event_time          TIMESTAMP,
  fpid                BIGINT,
  app_id              VARCHAR(16),
  device_platform     VARCHAR(16),
  country_code        VARCHAR(8),
  gameserver_id       VARCHAR(16),
  app_language        VARCHAR(8),
  device_level        SMALLINT,
  city_level          SMALLINT,
  amount              DECIMAL(12,4),
  is_white_user       SMALLINT,
  new_app_id          VARCHAR(16),
  payment_processor   VARCHAR(32),
  iap_product_id      VARCHAR(128),
  iap_product_name    VARCHAR(128),
  base_price          VARCHAR(16),
  iap_product_name_cn VARCHAR(128),
  app_version         VARCHAR(32),
  currency            VARCHAR(8),
  order_id            VARCHAR(64),
  ts                  BIGINT
)
DISTSTYLE AUTO
SORTKEY (event_time);
