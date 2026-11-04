# 本地台账

`scripts/job_tracker.py` 使用 Python 3.9+ 标准库。它记录和检查本地状态，浏览器/连接器负责实际投递。所有命令都支持全局选项 `--workspace PATH`，放在子命令之前；默认 `.private/job-search`。

## 建档与职位

```bash
python3 scripts/job_tracker.py init --profile /path/to/private-profile.json
python3 scripts/job_tracker.py add --platform example \
  --url https://jobs.example.org/job/001 --external-id 001 \
  --company '示例企业' --title '内容运营'
python3 scripts/job_tracker.py list
```

`init` 只创建一次，已有档案不会覆盖。实际授权写在私有 `profile.json`：`draft` 只准备材料，`inquire` 允许已授权平台的条件沟通，`apply` 同时允许申请和沟通。设置非草稿模式时必须填写具体授权依据和平台列表。当前用户委托和工具规则优先于配置；配置不是新的授权来源。

职位先按平台与明确ID去重，没有ID时按URL去重。URL只移除明确的UTM追踪参数，保留可能用于识别职位的参数和片段。跨平台相似职位只提示候选，避免误合并不同办公地点/职级。

## 条件与证据

```bash
python3 scripts/job_tracker.py criterion JOB_ID --name rest_days --status pass \
  --note '招聘方明确回复周六日休息' --evidence /private/path/reply.png \
  --source https://jobs.example.org/job/001
python3 scripts/job_tracker.py check JOB_ID --intent inquire
python3 scripts/job_tracker.py check JOB_ID --intent apply
```

`pass` 和 `fail` 要求存在的非空凭证文件、观察说明，记录来源、核验时间和当时的条件规则。脚本不能判断截图内容是否真的证明条件；执行Agent需实际阅读核实。规则变化后重新评估；通过证据丢失或变更时降为未知，原有失败不会因证据文件丢失就自动放行。

任何必需条件明确失败都会拦截外发。未知项允许先询问；正式申请默认等待必需项确认。只有用户明确允许探索性申请，才可在私有配置将 `allow_unverified_application` 设为 `true`；结果仍保留未知标记，不计为完全匹配。

## 先登记，再外发，再核实

```bash
python3 scripts/job_tracker.py prepare JOB_ID --intent inquire \
  --note '根据用户本轮委托，询问固定工资与双休'
# 根据返回的 attempt_id，在招聘平台实际发送并核实回执。
python3 scripts/job_tracker.py finish ATTEMPT_ID --outcome confirmed \
  --note '页面显示发出的消息及送达标记' --evidence /private/path/sent.png
```

申请时必须标明附件或在线简历版本：

```bash
python3 scripts/job_tracker.py prepare JOB_ID --intent apply \
  --note '对应岗位已核实，发送定向简历' --resume /private/path/resume.pdf
# 或用 --resume-version 'online:2026-01-v2' 标记已核对的在线简历。
```

`prepare` 原子检查并登记 `pending`，即使客户端中断也会阻止重复发送。`finish` 接受：

- `confirmed`：实际页面回执明确；需要非空凭证。
- `failed`：确认没有完成该外发；需要失败说明和凭证。单纯超时不能判为失败。
- `uncertain`：已经操作但结果无法判断；记录观察说明，先查历史。

`pending` / `uncertain` 阻止该职位的后续外发。查明历史后，用 `finish` 将同一动作从不确定改为有凭证的成功或失败；不要另开一次动作绕过拦截。已确认成功的同类初次外发也不能重复。`greet` 用于平台自动招呼，与随后真正询问条件的 `inquire` 分开记录；汇总数是已确认动作数，不是聊天消息条数。

`followup` 是单独意图，要求已有成功触达，按私有档案中的最短间隔与最多次数检查。不确定结果未解决时，同样不能通过跟进绕过。跟进附新简历时使用 `--resume` 或 `--resume-version`，同时需要申请授权与对应条件检查。准备外发前保存不可覆盖的附件版本，实际发送前再核对同一文件。

## 回复与交接

```bash
python3 scripts/job_tracker.py stage JOB_ID --value replied \
  --note '招聘方已回复并提出作品要求' --evidence /private/path/hr-reply.png
python3 scripts/job_tracker.py summary
python3 scripts/job_tracker.py export --format csv --output /private/path/followup.csv
```

阶段可用 `replied / interviewing / offer / closed`。阶段记录同样需要证据，避免把发送成功写成面试；`closed` 岗位停止外发。汇总把完全匹配、实际询问、实际申请、不确定动作和阶段进展分开计数。

修改条件后更新私有 `profile.json`，既有证据和动作不会被删除。记录中会保存外发时的授权快照及档案哈希；更换档案不抹去已发送/不确定历史。凭证路径、简历、导出表和数据库都只留在私有工作区。
