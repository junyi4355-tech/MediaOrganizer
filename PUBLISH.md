# 如何发布（开源）

仓库已经建好在本地（`main` 分支，首次提交已完成），**只差推到远端**。
本机没有 GitHub/Gitee 的登录凭据，所以最后一步需要你在网页上建一个空仓库，然后执行一条 push。

---

## 方式 A：GitHub（网页建库 + push，最省事）

1. 浏览器打开 <https://github.com/new>
2. Repository name 填 `MediaOrganizer`，**不要**勾选 "Add a README / .gitignore / license"（本地都有了）
3. 可见性按需选 **Public**（公开即开源）或 Private
4. 建好后在项目目录执行（把 `<你的用户名>` 换成你的 GitHub 用户名）：

```powershell
cd <本项目目录>
git remote add origin https://github.com/<你的用户名>/MediaOrganizer.git
git push -u origin main
```

> 第一次 push 会弹出浏览器让你登录授权，或要求输入 Personal Access Token。

---

## 方式 B：Gitee / 码云（国内访问快）

1. 打开 <https://gitee.com/projects/new>
2. 填仓库名 `MediaOrganizer`，**不要**初始化 README
3. 然后：

```powershell
git remote add origin https://gitee.com/<你的用户名>/media-organizer.git
git push -u origin main
```

---

## 方式 C：装 GitHub CLI 后一条命令建库并推

```powershell
winget install --id GitHub.cli
gh auth login
cd <本项目目录>
gh repo create MediaOrganizer --public --source . --remote origin --push
```

---

## 推送前建议确认的 3 件事

1. **作者署名**。首次提交用的是占位身份。设好你自己的再改：
   ```powershell
   git config user.name  "你的名字"
   git config user.email "你的邮箱"
   git commit --amend --reset-author --no-edit
   ```
2. **LICENSE 署名**。`LICENSE` 里目前写的是 `MediaOrganizer contributors`，
   想换成你的名字就改这一行；同时 README 底部的许可章节不用动。
3. **不要再把 `release/` 提交进去**。它是打包产物（104MB 文件夹 + 41MB zip），已在
   `.gitignore` 里。要分发给用户，用 GitHub 的 **Releases** 挂附件：
   - 网页：仓库 → Releases → Draft a new release → Tag `v1.0.0` → 上传
     `release/MediaOrganizer_v1.0.zip` → Publish
   - 或用 CLI：`gh release create v1.0.0 "release/MediaOrganizer_v1.0.zip" -t "v1.0.0"`

---

## 已完成的本地准备

| 项 | 状态 |
|---|---|
| `git init -b main` | 完成 |
| 首次提交 | 完成（15 个文件，仅源码/文档/测试/截图） |
| `.gitignore` | 忽略 `build/ dist/ release/ __pycache__/ *.log _*result.txt MediaOrganizer.ini` 等 |
| `.gitattributes` | 统一换行，避免 Windows 用户 clone 后满屏改动 |
| `.git` 之外无残留 | 已核查（无 `*.log` / `_*result.txt` / `*.pyc` / `*.ini`） |
| 隐私审计 | 已扫描源码与文档：无本机路径、无用户名、无 token/密钥 |

## 提交历史

```
git log --oneline
```

> 提示：仓库里不放打包产物；用户下载用 Releases 的附件。别人要自己构建就跑
> `.\build.ps1`（需要 `py -m pip install pyqt6 pillow pyinstaller`）。
