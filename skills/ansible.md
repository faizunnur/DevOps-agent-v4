## CRITICAL: Inventory and hosts: must always match

The `hosts:` in playbook.yml must EXACTLY match the group name in the inventory.

```yaml
# In pipeline: ansible-playbook -i "1.2.3.4," playbook.yml
# The comma creates a single-host group called "all"
# So the playbook MUST use hosts: all

# WRONG — causes "Could not match supplied host pattern, ignoring: web_servers"
- name: Deploy app
  hosts: web_servers   # ← this group doesn't exist in the -i "1.2.3.4," inventory

# CORRECT
- name: Deploy app
  hosts: all           # ← always use "all" when inventory is -i "IP,"
```

**Rule: Always use `hosts: all` in playbook.yml when inventory is a raw IP with comma.**
The warning "Could not match supplied host pattern" means hosts: group ≠ inventory group.
This causes the entire play to skip silently — ansible exits 0 but does nothing.

# Ansible Best Practices

## Inventory format — always use comma after IP
```
ansible-playbook -i "1.2.3.4," --private-key /tmp/key -u ubuntu playbook.yml
```

## CRITICAL: Callback Plugins
NEVER set `ANSIBLE_STDOUT_CALLBACK=community.general.yaml` or use the `community.general.yaml` callback plugin. It has been removed in newer Ansible versions. If you need YAML output, use `ANSIBLE_STDOUT_CALLBACK=yaml` (which maps to `ansible.builtin.yaml` or `ansible.builtin.default` with `result_format=yaml`).

## Always use become: yes for system tasks
## Always define handlers for service restarts
## Use copy module for files, template for Jinja2
## Use service module with enabled: yes
## Use apt with update_cache: yes and cache_valid_time: 3600

## Wait for SSH before running playbook
```bash
for i in $(seq 1 30); do
  ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 \
    -i /tmp/key ubuntu@SERVER_IP echo ok && break || sleep 10
done
```

## Common patterns
- Files: copy src to dest with mode and owner
- Services: state=started enabled=yes
- Packages: state=present with apt
- Directories: file module with state=directory

## CRITICAL: YAML Quoting Rules
These mistakes cause "YAML parsing failed: Colons in unquoted values" errors:

### Rule 1: ALWAYS quote name: values that contain colons
```yaml
# WRONG — colon in value causes YAML parse error
- name: Install docker-ce: latest version

# CORRECT — quoted
- name: "Install docker-ce: latest version"
```

### Rule 2: ALWAYS quote shell commands in name: fields
```yaml
# WRONG
- name: Run echo 'name: ansible'

# CORRECT
- name: "Run echo name ansible"
# Better — avoid colons in name fields entirely
- name: Echo test
```

### Rule 3: Never put Jinja2 {{ }} in name: without quotes
```yaml
# WRONG
- name: Deploy {{ app_name }}: production

# CORRECT
- name: "Deploy {{ app_name }}: production"
```

### Rule 4: shell/command values with colons need quoting too
```yaml
# WRONG
- shell: echo name: test

# CORRECT  
- shell: "echo name: test"
# Or use |
- shell: |
    echo 'name: test'
```

### Quick fix rule:
If error says "Colons in unquoted values" at line N:
→ Find line N in the playbook
→ Quote the entire value with double quotes
→ Or rewrite the name: to remove the colon entirely

## CRITICAL: Deploying index.html — conditional pattern (supports new AND existing repos)

NEVER hardcode `content: |` for index.html — updates pushed via the bot will be ignored.
NEVER use `src: ../index.html` unconditionally — it FAILS on new repos that have no index.html yet.

### CORRECT: Use stat to check if index.html exists in the repo, then choose src or default

```yaml
# Step 1: check if index.html was committed to the repo
- name: Check if index.html exists in repo
  stat:
    path: "{{ playbook_dir }}/../index.html"
  delegate_to: localhost
  register: index_html_stat

# Step 2a: copy from repo if it exists (respects bot /update changes)
- name: Deploy index.html from repo
  copy:
    src: ../index.html
    dest: "{{ web_root }}/index.html"
    owner: www-data
    group: www-data
    mode: "0644"
  when: index_html_stat.stat.exists
  notify: reload nginx

# Step 2b: write a default page only if no index.html in repo yet
- name: Deploy default index.html
  copy:
    content: |
      <!DOCTYPE html>
      <html lang="en">
      <head><meta charset="UTF-8"><title>{{ app_name }}</title></head>
      <body style="font-family:Arial,sans-serif;display:flex;justify-content:center;
                   align-items:center;height:100vh;margin:0;background:#f0f2f5;">
        <div style="text-align:center;background:white;padding:40px 60px;
                    border-radius:8px;box-shadow:0 2px 12px rgba(0,0,0,.1);">
          <h1>{{ app_name }}</h1>
          <p>Deployed with Ansible</p>
        </div>
      </body>
      </html>
    dest: "{{ web_root }}/index.html"
    owner: www-data
    group: www-data
    mode: "0644"
  when: not index_html_stat.stat.exists
  notify: reload nginx
```

### Rule: Path resolution in GitHub Actions
- Ansible runs from the `ansible/` directory
- `playbook_dir` = `/home/runner/work/<repo>/<repo>/ansible`
- `playbook_dir/../index.html` = repo root `index.html`
- The `stat` check is LOCAL (on the runner) — this is correct for `copy` module src paths

## CRITICAL: become_user vs become: yes
```yaml
# become: yes  → run task as ROOT (needed for apt, systemd, file permissions)
# become_user: ubuntu → run task as a specific non-root user (e.g. PM2, app startup)

# WRONG — become_user alone does not grant sudo, causes permission errors
- name: Start app
  shell: pm2 start app.js
  become_user: ubuntu         # ← alone this is not sufficient

# CORRECT — use become: yes at play level, become_user only for user-specific tasks
- name: Start app with PM2
  shell: pm2 start app.js --name app
  become: yes
  become_user: ubuntu         # ← with become:yes this runs as ubuntu via sudo

# CORRECT — for system tasks always just become: yes (runs as root)
- name: Install nginx
  apt:
    name: nginx
  become: yes
```
**Rule: Set `become: yes` at play level for all system tasks. Only add `become_user` when running a command as a specific non-root user (e.g. PM2 under ubuntu).**