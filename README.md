# 𝚇𝙲𝙾𝚁𝙴® GIT — V1

Telegram-based GitHub repository manager for beginners and developers.

## Included
- GitHub OAuth Device Flow login (verification code) and Personal Access Token (PAT) login
- Encrypted credential storage for both authentication methods
- Account details and repository counts
- Create public/private repositories
- Optional description, README and .gitignore template
- List and open repositories
- Upload ZIP projects, preserving relative folder structure
- Upload a single file
- Download a repository ZIP
- Browse and delete repository files
- Edit repository description, delete repository, retrieve URL

## Setup
1. Create a Telegram bot using BotFather.
2. Copy `.env.example` to `.env`.
3. Install Python 3.11+ and dependencies:
   `pip install -r requirements.txt`
4. Generate an encryption key:
   `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
5. Put `BOT_TOKEN` and `ENCRYPTION_KEY` in `.env`.
6. For OAuth login, create a GitHub OAuth App, enable **Device Flow** in its settings, and put its **Client ID** in `GITHUB_CLIENT_ID`. No client secret is required for Device Flow. The app's authorization scopes are `repo`, `user:email`, and `delete_repo`.
7. Add `GITHUB_CLIENT_ID` to Render/Railway/VPS environment variables as well. If it is blank, PAT login continues to work and OAuth is hidden behind a configuration warning.
8. Run: `python -m xcore_git.main`

Keep `.env`, the SQLite database and encryption key private. Use persistent storage in deployment. Losing the encryption key makes saved tokens unreadable.

## GitHub permissions
### Authentication setup

The connection menu offers two choices:
- **Login with GitHub:** the bot requests a one-time device code. The user opens `https://github.com/login/device`, enters the code, and approves the OAuth App. The bot polls GitHub at the interval returned by GitHub and stores the resulting token encrypted.
- **Personal Access Token:** the user sends a PAT in the private Telegram chat. The bot attempts to delete the message immediately and stores the token encrypted.

For the OAuth App, enable Device Flow in the app settings. The OAuth scopes requested are `repo`, `user:email`, and `delete_repo`, because the existing bot supports private repository access, email display, and repository deletion. Ask users to review the authorization screen before approving. Keep the encryption key stable across restarts and redeployments.

For PAT login, use a fine-grained token with Metadata read, Contents read/write, and Administration permissions for repository creation/deletion and repository settings. Limit repository access to only the repositories the user needs. Classic tokens may require `repo` and `delete_repo` scopes.

## ZIP behavior
The bot checks ZIP paths, file count and extracted size, preserves folder hierarchy and removes a single common outer wrapper folder where appropriate. It excludes common secret files such as `.env`, private keys and credential files. Review project contents before upload. The bot does not guarantee deployment success; it preserves the project structure but cannot configure hosting environment variables or fix application code.

## V1 limitations
Folder-specific upload, multiple separate Telegram attachments in one upload session, inline file editing, branch selection, pull requests, Actions and deployment integrations are not included yet. OAuth Device Flow uses an OAuth App with Device Flow enabled; expiring OAuth refresh-token rotation is not yet implemented, so do not enable expiring user tokens for this OAuth App until refresh handling is added. ZIP upload uses GitHub's Git Data API to create a tree and a commit. GitHub API and Telegram file-size limits still apply.
