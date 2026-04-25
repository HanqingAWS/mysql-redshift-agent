-- Aurora MySQL 版建表 DDL
-- 表：ads_thor_fin_payment_iap_data_new（IAP 支付流水，24 列）
-- 注意：按讨论，表不放在 dw schema 下；表本身就是数据库 dw 里的裸表
-- 这样客户端 SQL 里的 `dw.ads_thor_fin_payment_iap_data_new` 在 Redshift 侧需要改写为 `ads_thor_fin_payment_iap_data_new`

DROP TABLE IF EXISTS ads_thor_fin_payment_iap_data_new;

CREATE TABLE ads_thor_fin_payment_iap_data_new (
  game_name           VARCHAR(64),
  uid                 VARCHAR(32),
  transaction_id      VARCHAR(64),
  event_time          DATETIME,
  fpid                BIGINT,
  app_id              VARCHAR(16),
  device_platform     VARCHAR(16),
  country_code        VARCHAR(8),
  gameserver_id       VARCHAR(16),
  app_language        VARCHAR(8),
  device_level        SMALLINT,
  city_level          SMALLINT,
  amount              DECIMAL(12,4),
  is_white_user       TINYINT,
  new_app_id          VARCHAR(16),
  payment_processor   VARCHAR(32),
  iap_product_id      VARCHAR(128),
  iap_product_name    VARCHAR(128),
  base_price          VARCHAR(16),
  iap_product_name_cn VARCHAR(128),
  app_version         VARCHAR(32),
  currency            VARCHAR(8),
  order_id            VARCHAR(64),
  ts                  BIGINT,
  KEY idx_event_time (event_time),
  KEY idx_uid (uid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
