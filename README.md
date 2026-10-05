# 动物园谱系与繁育协调

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8308`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

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
python3 app.py --db ./data.db --port 8308
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `animal`：个体谱系；`pairing`：配对建议；`transfer`：机构和运输记录。

## 谱系修正

登记员（`registrar`）可对个体提交`correct_pedigree`动作修正父母记录：

```json
{"action": "correct_pedigree", "data": {"sire_id": "...", "dam_id": "...", "reason": "旧档案订正"}}
```

- `sire_id`/`dam_id`至少提供其一，传`null`表示清除；`reason`必填。新父母必须是已登记个体，且不能形成谱系环。
- 修正会沿后代重算近交系数（亲缘系数按谱系图递归计算，未知父母按始祖处理），写回个体`data.inbreeding`。
- 涉及受影响个体且已批准的配对建议会重新校验：超过阈值`0.125`的退回`proposed`待审，并在`data.review_reason`中注明是哪次谱系修正（`corrected_animal_id`、`correction_reason`）引起的。
- 个体状态不变；修正、后代重算和配对退回在同一事务中写入，失败时保留原记录，可直接重试。

## 运输对账

对`transfer`提交`reconcile`动作按外部编号对账：

```json
{"action": "reconcile", "data": {"external_animal_id": "EXT-9"}}
```

- 外部编号取自请求或记录中的`external_animal_id`/`animal_id`，只匹配本机构已登记个体（按`external_id`或内部编号）。
- 对不上时记录转入`suspended`挂起（`?status=suspended`可查询），不能继续`authorize`；个体补登记后再次提交`reconcile`即可继续，匹配成功回到`planned`并解析出内部`animal_id`。
- 对账是单记录更新，失败保留原记录，可接着重试。

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

谱系系数是简化亲缘规则，不替代专业谱系软件、遗传咨询或法定动物运输许可。
