# Docker Deployment Skill

## Overview
Deploy a Dockerized app on AWS EC2 using Ansible.
Ansible installs Docker, adds user to docker group, builds image, runs container.

## Dockerfile (nginx — ALWAYS use this robust pattern)
```dockerfile
FROM nginx:alpine
# Copy everything into a staging area
COPY . /tmp/app/
# Intelligently locate index.html: prefer html/ subdir, fall back to root
RUN if [ -d /tmp/app/html ] && [ -f /tmp/app/html/index.html ]; then \
      cp -r /tmp/app/html/. /usr/share/nginx/html/; \
    else \
      find /tmp/app -maxdepth 1 -name '*.html' -exec cp {} /usr/share/nginx/html/ \; ; \
    fi
EXPOSE 80
```
This pattern ensures the correct HTML is always served regardless of whether the user uploaded `index.html` to the repo root or inside an `html/` subfolder.

## Ansible Playbook Pattern
```yaml
---
- name: Deploy Docker App
  hosts: all
  become: yes
  vars:
    project: "{{ lookup('env', 'PROJECT_NAME') }}"
    ansible_user: ubuntu

  tasks:
    - name: Install dependencies
      apt:
        name: [apt-transport-https, ca-certificates, curl, gnupg, lsb-release]
        state: present
        update_cache: yes

    - name: Add Docker GPG key
      apt_key:
        url: https://download.docker.com/linux/ubuntu/gpg
        state: present

    - name: Add Docker repo
      apt_repository:
        repo: "deb [arch=amd64] https://download.docker.com/linux/ubuntu jammy stable"
        state: present

    - name: Install Docker CE
      apt:
        name: [docker-ce, docker-ce-cli, containerd.io]
        state: present
        update_cache: yes

    - name: Start and enable Docker
      service:
        name: docker
        state: started
        enabled: yes

    - name: Add ubuntu user to docker group
      user:
        name: ubuntu
        groups: docker
        append: yes

    - name: Reset SSH connection so group change takes effect
      meta: reset_connection

    - name: Wipe old app files to prevent stale file serving
      file:
        path: /opt/app
        state: absent
      ignore_errors: yes

    - name: Recreate app directory
      file:
        path: /opt/app
        state: directory
        mode: '0755'

    - name: Copy app files (full repo sync)
      copy:
        src: ../
        dest: /opt/app/
        mode: '0755'

    - name: Stop existing container
      shell: |
        docker stop app || true
        docker rm app || true

    - name: Remove old image to force rebuild
      shell: docker rmi app:latest || true

    - name: Build image
      shell: docker build --no-cache -t app:latest /opt/app/

    - name: Run container
      shell: docker run -d --name app -p 80:80 --restart always app:latest

    - name: Wait for container to be ready
      pause:
        seconds: 5

    - name: Verify container is running
      shell: docker ps --filter name=app --filter status=running --format '{{ "{{" }}.Names{{ "}}" }}'
      register: result
      retries: 3
      delay: 5
      until: result.stdout != ""
      become: yes
```

## CRITICAL RULES
- ALWAYS add ubuntu user to docker group — otherwise SSH verify step gets "permission denied"
- ALWAYS add `meta: reset_connection` after adding user to docker group — without this the group change won't take effect in the same playbook run
- Always install Docker CE (not docker.io)
- Always add `|| true` to stop/rm commands
- Use `--restart always` so container survives reboots
- Copy entire project to /opt/app/ so Dockerfile can access all files
- Build image on server — do NOT pull from registry unless specified
- Port mapping: always -p 80:80 for web apps
- Verify step: use `docker ps --filter` NOT `docker ps | grep` — more reliable
- Verify step: use `become: yes` and retries — container may take a few seconds to start
- NEVER run docker commands as ubuntu without become:yes OR without the user being in docker group

## Dockerfile Rules
- Use alpine/slim variants (nginx:alpine, node:alpine, python:slim)
- For nginx: ALWAYS use the robust COPY pattern above (copy all to /tmp/app, then find index.html).
  NEVER use `COPY html/ /usr/share/nginx/html/` directly — this FAILS if index.html is at the repo root.
  NEVER use `COPY . /usr/share/nginx/html/` blindly — this fails if files are nested.
  The only correct pattern is: COPY . /tmp/app/ → then shell find/cp logic as shown above.
- For node apps: RUN npm install before COPY src
- EXPOSE the correct port

## Common Errors and Fixes
| Error | Cause | Fix |
|-------|-------|-----|
| `permission denied /var/run/docker.sock` | ubuntu not in docker group | Add user to docker group + meta: reset_connection |
| `docker: command not found` | Docker not installed | Install docker-ce (not docker.io) |
| `port already in use` | Old container still running | Always docker stop/rm before run |
| Container exits immediately | App crash or wrong CMD | Check Dockerfile CMD, check app logs |