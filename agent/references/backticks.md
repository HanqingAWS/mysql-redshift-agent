# 规则：反引号 → 双引号

MySQL 用反引号引用标识符（表名、列名、保留字）：
```sql
SELECT `order`, `amount` FROM `orders`
```

Redshift（PostgreSQL 方言）不支持反引号，改用双引号：
```sql
SELECT "order", "amount" FROM "orders"
```

## 注意

- **字符串**用单引号不变：`WHERE name='alice'`
- Redshift 默认把未引用的标识符**转成小写**，所以 `SELECT UID` 和 `SELECT uid` 等价；但带双引号的 `"UID"` 则保留大小写
- 本项目表名和列名都是小写，可以保持不加引号也能跑，但安全做法是把反引号统一换成双引号
