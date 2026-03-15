# Python/FastAPI/Flask Deployment Skill

## Ansible Playbook (FastAPI/Flask via gunicorn + nginx)
```yaml
---
- name: Configure Python app
  hosts: all
  become: yes
  vars:
    app_dir: /opt/app
    app_port: 8000
    app_user: ubuntu
  tasks:
    - name: Update apt cache
      apt:
        update_cache: yes

    - name: Install Python and deps
      apt:
        name:
          - python3
          - python3-pip
          - python3-venv
          - nginx
        state: present

    - name: Create app directory
      file:
        path: "{{ app_dir }}"
        state: directory
        owner: "{{ app_user }}"
        mode: '0755'

    - name: Copy app files
      copy:
        src: app/
        dest: "{{ app_dir }}/"
        owner: "{{ app_user }}"
        mode: '0644'

    - name: Create virtualenv and install requirements
      pip:
        requirements: "{{ app_dir }}/requirements.txt"
        virtualenv: "{{ app_dir }}/venv"
        virtualenv_command: python3 -m venv

    - name: Create systemd service
      copy:
        content: |
          [Unit]
          Description=Python App
          After=network.target

          [Service]
          User={{ app_user }}
          WorkingDirectory={{ app_dir }}
          ExecStart={{ app_dir }}/venv/bin/gunicorn -w 4 -b 0.0.0.0:{{ app_port }} main:app
          Restart=always

          [Install]
          WantedBy=multi-user.target
        dest: /etc/systemd/system/app.service

    - name: Start and enable app service
      systemd:
        name: app
        state: started
        enabled: yes
        daemon_reload: yes

    - name: Configure nginx
      copy:
        content: |
          server {
              listen 80;
              server_name _;
              location / {
                  proxy_pass http://localhost:{{ app_port }};
                  proxy_set_header Host $host;
                  proxy_set_header X-Real-IP $remote_addr;
              }
          }
        dest: /etc/nginx/sites-enabled/app

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

## Notes
- App runs via gunicorn on port 8000
- nginx reverse proxies 80 → 8000
- Virtualenv in /opt/app/venv
- Systemd manages the process
- Main app file should be main.py with app = Flask(__name__) or FastAPI()

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

### Alternative — write app files inline:
```yaml
- name: Write main.py
  copy:
    content: |
      from fastapi import FastAPI
      app = FastAPI()

      @app.get("/")
      def root():
          return {"status": "ok"}
    dest: "{{ app_dir }}/main.py"
```

### Also write requirements.txt inline if not in repo:
```yaml
- name: Write requirements.txt
  copy:
    content: |
      fastapi
      uvicorn
      gunicorn
    dest: "{{ app_dir }}/requirements.txt"
```

**Rule: Never use `src: app/` unless an `app/` folder is actually committed in the repo.**