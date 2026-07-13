# Docker Image Tag Sync Automation

This automation system synchronizes Docker image tags across multiple API service branches in a single GitHub repository. When a new Docker image is built and pushed to a container registry, this tool automatically updates the image tag in deployment YAML files across all target branches and creates Pull Requests for each update.

## Prerequisites

To run this automation, you need:
1. A GitHub App to authenticate and perform repository actions (branches, commits, pull requests).
2. The following Repository Secrets set up in your repository settings:
   - `APP_ID`: The unique identifier of your GitHub App.
   - `APP_PRIVATE_KEY`: The PEM formatted private key of the GitHub App.

## Setup Guide

### 1. Create a GitHub App
1. Go to **Settings** > **Developer Settings** > **GitHub Apps** > **New GitHub App**.
2. Set the following permissions:
   - **Repository Permissions**:
     - `Contents`: Read & write (to commit tag updates)
     - `Pull requests`: Read & write (to create pull requests)
     - `Metadata`: Read-only (required default)
3. Under **Private keys**, generate a private key and download the `.pem` file.
4. Install the GitHub App on your repository.

### 2. Add Secrets to the Repository
1. Navigate to your repository page on GitHub.
2. Go to **Settings** > **Secrets and variables** > **Actions**.
3. Create the following repository secrets:
   - `APP_ID`: Paste your App ID.
   - `APP_PRIVATE_KEY`: Paste the full contents of the `.pem` private key file.

### 3. Configure `updater-config.yml`
Edit `updater-config.yml` on the `main` branch to list the target branches and YAML file paths. See the [Configuration Reference](#configuration-reference) below.

---

## Usage

### 1. Automatic Trigger (Build Pipeline)
At the end of your image build pipeline, send a `repository_dispatch` trigger:
```yaml
- name: Trigger Image Tag Sync
  uses: peter-evans/repository-dispatch@v3
  with:
    token: ${{ secrets.SYNC_BOT_PAT || secrets.GITHUB_TOKEN }}
    repository: ${{ github.repository }}
    event-type: image-built
    client-payload: '{"new_tag": "${{ github.sha }}", "environment": "production"}'
```

### 2. Manual Trigger (GitHub Actions UI)
1. Go to the **Actions** tab in your repository.
2. Select **Sync Docker Image Tags**.
3. Click **Run workflow**, enter the `new_tag`, choose the `environment`, and run.

### 3. Manual Trigger (GitHub CLI)
```bash
gh workflow run sync-image-tag.yml -f new_tag="v1.2.3" -f environment="production" -r main
```

---

## Configuration Reference

The `updater-config.yml` configures target files and PR templates:

| Field | Type | Description |
|---|---|---|
| `registry` | String | The container registry prefix (e.g. `registry.example.com/platform`). |
| `image_field_path` | String | Dot-notation path to the image field inside deployment files. Supports nested elements like `spec.template.spec.containers[0].image` or simple `image`. |
| `targets` | Array | List of target configurations. Each target requires: <br> - `branch`: Branch to target.<br> - `file`: Path to the YAML file on that branch.<br> - `description`: (Optional) Log name. |
| `settings` | Object | Optional settings overrides: <br> - `pr_title_template`: PR title template (supports `{new_tag}`).<br> - `pr_body_template`: Multiline PR body (supports `{new_tag}`, `{old_tag}`, `{triggered_by}`).<br> - `feature_branch_template`: Feature branch name template (supports `{new_tag}`).<br> - `pr_labels`: Array of labels to apply.<br> - `auto_merge`: Boolean (defaults to `false`). |

---

## How It Works
1. **Load Config:** Loads target definitions from `updater-config.yml` on `main`.
2. **Fetch and Compare:** For each target, the current tag is fetched and compared to the target `new_tag`. If they match, it skips.
3. **Preserve & Replace:** The script replaces the image tag without modifying rest of the file layout or removing comments.
4. **Create PR:** Creates a dedicated feature branch from target HEAD, pushes the updated file, and creates/updates a Pull Request.
5. **Output Summary:** Publishes a Markdown table summary to stdout and the GitHub Action run summary page.

---

## Troubleshooting
- **API permissions issue (403/404):** Verify the GitHub App is installed on the target repository and contains `Contents: Write` and `Pull Requests: Write` permissions.
- **Skipped updates:** If a target's tag already matches the `new_tag`, the run is skipped for that target (this prevents duplicate branch/PR creation).
- **Target branch fails but run continues:** The orchestrator runs target updates isolated; one branch failing (e.g. missing target file) does not abort the other updates.

---

## Adding a New API
To add a new API service target branch to the synchronization sequence, simply add a new object to the `targets` list in `updater-config.yml`:
```yaml
  - branch: api-service-new-feature
    file: k8s/deployment.yml
    description: New API Service Feature
```
