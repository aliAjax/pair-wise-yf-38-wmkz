# 基因组数据访问治理

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8304`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8304
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `dataset`：受控数据集；`application`：访问申请；`grant`：限时数据使用凭证。

## 凭证治理规则

- **激活核对**：`grant/activate` 时关联申请必须为 `approved`；凭证的受让人（`recipient`）、数据集（`dataset_id`）、用途（`purpose`）必须与申请一致；凭证期限不得超过审批截止（申请的 `expires_at`）。同一申请只允许一张 `active` 凭证。
- **到期续期**：仅 `active` 且尚未到期的凭证可 `renew`，新期限必须晚于当前到期日，且不能超过原审批截止（申请的 `original_expires_at`）。超出审批截止的续期会被拒绝，须先由委员会对申请执行 `reapprove`（同样需要三名不同委员批准并延长审批截止）；落在原审批截止之外的续期会被标记 `beyond_original_approval`。
- **暂停与恢复**：申请 `suspend` 后，其所有 `active` 凭证自动 `freeze`；`resume` 后仅尚未到期的凭证 `unfreeze` 回 `active`，已过期的凭证直接置为 `expired`，不会放回。
- **操作留痕**：续期、冻结、解冻、到期等动作全部写入审计时间线，可通过 `GET /api/audit?entity_id=<id>` 或演示页面查看。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

数据目录和授权凭证是治理流程演示，不包含真实数据下载、加密或机构身份联邦。
