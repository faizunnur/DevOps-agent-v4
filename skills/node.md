# Node.js Deployment Skill

## Ansible Playbook
```yaml
---
- name: Configure Node.js app
  hosts: all
  become: yes
  vars:
    app_dir: /opt/app
    app_port: 3000
  tasks:
    - name: Update apt cache
      apt:
        update_cache: yes

    - name: Install Node.js 20.x
      shell: |
        curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
        apt-get install -y nodejs
      args:
        creates: /usr/bin/node

    - name: Install PM2
      npm:
        name: pm2
        global: yes
        state: present

    - name: Install nginx
      apt:
        name: nginx
        state: present

    - name: Create app directory
      file:
        path: "{{ app_dir }}"
        state: directory
        mode: '0755'

    - name: Copy app files
      copy:
        src: app/
        dest: "{{ app_dir }}/"
        mode: '0644'

    - name: Install npm dependencies
      npm:
        path: "{{ app_dir }}"
        state: present

    - name: Start app with PM2
      shell: |
        pm2 delete app 2>/dev/null || true
        pm2 start {{ app_dir }}/index.js --name app
        pm2 save
        pm2 startup systemd -u ubuntu --hp /home/ubuntu
      become_user: ubuntu

    - name: Configure nginx reverse proxy
      copy:
        content: |
          server {
              listen 80;
              server_name _;
              location / {
                  proxy_pass http://localhost:{{ app_port }};
                  proxy_http_version 1.1;
                  proxy_set_header Upgrade $http_upgrade;
                  proxy_set_header Connection 'upgrade';
                  proxy_set_header Host $host;
                  proxy_cache_bypass $http_upgrade;
              }
          }
        dest: /etc/nginx/sites-enabled/app
        mode: '0644'

    - name: Remove default nginx config
      file:
        path: /etc/nginx/sites-enabled/default
        state: absent

    - name: Restart nginx
      service:
        name: nginx
        state: restarted
        enabled: yes
```

## Terraform ports
- 22 (SSH)
- 80 (HTTP via nginx reverse proxy)
- 3000 (optional direct access)

## Notes
- App runs on port 3000, nginx proxies port 80 → 3000
- Use PM2 for process management
- App files go in /opt/app/
- Always install Node 20.x LTS

## CRITICAL: Copy app files — src path must exist in repo

The `src: app/` path only works if there is an `app/` folder committed in the repo root.
If the repo has no `app/` folder, this task will fail with "Could not find or access".

### Preferred approach — clone/pull from GitHub on the remote server:
```yaml
- name: Pull app from repo
  git:
    repo: "https://github.com/{{ lookup('env', 'GITHUB_REPOSITORY') }}.git"
    dest: "{{ app_dir }}"
    version: HEAD
    force: yes
  environment:
    GIT_TERMINAL_PROMPT: "0"
```

### Alternative — write app inline with content: |
```yaml
- name: Write server.js
  copy:
    content: |
      const http = require('http');
      const server = http.createServer((req, res) => {
        res.writeHead(200);
        res.end('Hello from Node.js');
      });
      server.listen(3000);
    dest: "{{ app_dir }}/index.js"
```

**Rule: Never use `src: app/` unless an `app/` folder is actually committed in the repo.**