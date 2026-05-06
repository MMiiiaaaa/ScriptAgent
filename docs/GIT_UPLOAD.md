# 把本项目上传到 GitHub

## 已在仓库里做好的事

- **`.gitignore`**：忽略 `.env`、`*.log`、生成的报告/HTML、`test.sh`（避免把密钥脚本推上去）。
- **`test.sh.example`**：无密钥模板；本地复制为 `test.sh` 再填你自己的 Key。
- 若尚未提交：在项目根目录配置 Git 身份后执行下方「首次提交」。

## 1. 配置 Git 身份（只需一次）

```bash
cd /Users/miaaaa/Desktop/剧本
git config user.email "你的邮箱@example.com"
git config user.name "你的名字"
```

（或使用 `git config --global ...` 设为全局默认。）

## 2. 首次提交（若还没有 commit）

```bash
cd /Users/miaaaa/Desktop/剧本
git add -A
git status   # 确认没有 test.sh、.env、密钥文件
git commit -m "Initial commit: 剧本解析 Agent"
```

## 3. 在 GitHub 上新建空仓库

1. 打开 https://github.com/new  
2. Repository name 自定（例如 `script-agent-juben`）。  
3. **不要**勾选 “Add a README”（若本地已有提交可任选）。  
4. Create repository。

## 4. 关联远程并推送

把下面命令里的 **`你的用户名`** 和 **`仓库名`** 换成你的：

```bash
cd /Users/miaaaa/Desktop/剧本
git branch -M main
git remote add origin https://github.com/你的用户名/仓库名.git
git push -u origin main
```

首次 `push` 时浏览器或终端会提示登录 GitHub（Personal Access Token 或 SSH）。

### 使用 SSH（可选）

```bash
git remote add origin git@github.com:你的用户名/仓库名.git
git push -u origin main
```

## 5. 推送前务必自查

- [ ] **`test.sh` 不应出现在 `git status` 里**（已被忽略）。  
- [ ] 没有把 **`OPENAI_API_KEY`** 写进任何会提交的脚本或 Markdown。  
- [ ] 若仓库很大（例如包含无关的大目录），可先加入 `.gitignore` 再提交。

## 6. 以后修改后再上传

```bash
git add -A
git commit -m "说明本次改了什么"
git push
```
