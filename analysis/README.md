# 承接变更连续性分析（标准库实现）

输入为上级目录 `data/` 的九张交换表（合成资料），本目录脚本只读不写台账，
输出在 `out/`。请用隔离模式运行，避免工作目录中的同名模块被导入：

```sh
python3 -I report.py                      # 生成 out/report.md 与 out/report.json
python3 -I batch.py                       # 单独查看批次可承诺性
python3 -I -m unittest discover -s tests  # 口径测试（17 项）
```

## 模块分工

- `continuity.py`：表读取与全部口径函数——可认可履约（done / 指定 substitute /
  unverified 不计）、最长间隔与需要日、试办证据可比性、窗口资质+时点同意、
  延长台账（reserve/release 与确认截止日）、成组试接状态。
- `batch.py`：批次层判断——共享班组同日重叠窗口、成组整批约束、
  逐户逐项四分类（可切 / 暂留 / 旧岗兜底 / 联系家庭）、延长额度按需要日排序建议。
- `report.py`：逐户决策与 `out/report.md`、`out/report.json` 生成，
  含主管核准段（作用到每户每项的日期、接收范围与证据边界）。
- `tests/test_analysis.py`：口径与结论的回归测试。

## 关键口径（与 data/README 约定一致）

1. 资质/合同结束日不含当日；最长间隔按两次可认可完成之间的自然天数。
2. `unverified` 缺到屋凭证，不计连续性；`substitute` 仅当 `plans.substitute`
   指定时认可，且替代相邻间隔超过 `sub_days` 标记为超限。
3. Q 开头记录来自持单独试办资质的机构；只有同 care、同服务条件的 done 记录
   可作为可比样本外推，条件不同或不可核验的记录逐条列出并剔除。
4. 常规接收三要件：窗口当日已 recorded 的 approved 同意、有效资质、协议窗口覆盖；
   事后才批准的同意不能让早前窗口生效（如 X07/D 的 10-06 批准不覆盖 T05）。
5. 桥接（bridge）限时且可能只覆盖一个项目，不能当常规承接。
6. 交接台 `missed` 生效当日的候选窗口视为实际未接住，从可行路径剔除。
7. 延长报价过 `confirm_until` 后只承认已 reserve 且未 release 的额度；
   release 出来的额度也不能在截止日后重新指配。
8. 旧保障释放条件：新方有效签收（旧队 served 只用于连续性复盘），
   成组服务须试接期内整组各自有效签收并经正式交接确认；单项签收不能释放同组其余服务。
9. 逐户各有可行解不构成整批承诺：共享班组冲突与成组约束在 `batch.py` 统一拦截。

更换机构时，用新的照护节奏（plans/visit_history）与团队资料
（provider_terms/candidate_slots/consents 等）替换 `data/` 后重跑 `report.py`，
即可重新得到风险家庭、核准进度与无法按期覆盖的实际原因。
