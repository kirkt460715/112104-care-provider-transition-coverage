# 居家护理承接变更的连续性证据和切换核准

机构更换前的家庭服务约定、可比履约记录和各方交接凭证。 所有人员、服务机构和业务凭证均为合成资料。

合同结束日不含其当日。最长间隔是两次可认可服务完成之间的自然天数。group 代表同户必须协同的服务组，空值表示可单独履行。临时替代只由 substitute 指定。凭证记录包含旧队延长期、准备确认和实际履约，三种事实互不替代。

## 资料查阅

需要 Python 3.11.2，查看资料与运行资料检查只用标准库。CSV 使用 UTF-8，首行为字段名。空字段表示该项未填写或不适用，具体含义按各表约定。

```sh
python3 catalog.py
python3 -m unittest discover -s tests -v
```

例如，`python3 catalog.py --table plans` 可读取该表原始记录。资料检查核对结构、唯一键及部分业务引用。

## 交换表

- `plans.csv`。各户承诺中的服务频率、最长间隔、成组和桥接范围。 主键由 `scope` 确定。
- `visit_history.csv`。已核对的服务履约。Q 开头来自有单独有效试办资质的机构；常规承接期另外登记。条件列记录试办当时事实。 主键由 `visit` 确定。
- `provider_terms.csv`。承接使用范围与常规资质。旧队 A 和 B 到期范围以各自合同结束日为准。 主键由 `provider`、`care` 确定。
- `candidate_slots.csv`。候选承接方提供的窗口。team 是共享班组，同日重叠的安排不可重复使用。 主键由 `slot` 确定。
- `family_consents.csv`。对承接或临时服务的同意；同一范围可有更新。 主键由 `scope`、`provider`、`recorded` 确定。
- `extension_offer.csv`。旧保障延长意愿。shared_group 相同的可保留数量共享。 主键由 `offer` 确定。
- `trial_windows.csv`。家庭要求的新站试接期及同组必须认可的范围。 主键由 `group` 确定。
- `extension_events.csv`。已保留和已释放的旧保障窗口。reserve 后仍需按实际履约判断连续性，release 后额度才能用于其他家庭。 主键由 `event` 确定。
- `handover_proofs.csv`。从交接台收到的准备、双方确认、实际履约或未接住记录。 主键由 `event` 确定。
