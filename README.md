# Dota2 Team ID Fix

用于从 StarRocks 读取 Dota2 数据，检测同样 5 个选手在同一联赛或跨联赛中对应不同 Team ID 的情况，并在本地网页中查看、筛选和维护队伍映射 CSV。

## 安装依赖

```powershell
python -m pip install -r requirements.txt
```

## 启动网页

不要在代码中写入数据库密码。先设置环境变量：

```powershell
$env:STARROCKS_PASSWORD="your_password"
python .\dota_roster_web.py
```

然后打开：

```text
http://127.0.0.1:8000/
```

## 主要功能

- 从 `dota2_stats.players` 和 `dota2_stats.match_info` 读取完整数据。
- 支持同一联赛内同 5 人多 Team ID 检测。
- 支持跨联赛同 5 人不同 Team ID 检测。
- 支持按比赛结束时间筛选。
- 支持在网页内维护可编辑 CSV 表：`league_id, league_name, team_id, team_name`。

生成的 CSV 文件不会提交到 Git。
