# Nginx Deployment Skill

## Ansible Playbook
```yaml
---
- name: Configure nginx
  hosts: all
  become: yes
  tasks:
    - name: Update apt cache
      apt:
        update_cache: yes
        cache_valid_time: 3600

    - name: Install nginx
      apt:
        name: nginx
        state: present

    - name: Copy HTML file
      copy:
        src: html/index.html
        dest: /var/www/html/index.html
        mode: '0644'
        owner: www-data
        group: www-data

    - name: Remove default nginx config
      file:
        path: /etc/nginx/sites-enabled/default
        state: absent
      notify: restart nginx

    - name: Create nginx site config
      copy:
        content: |
          server {
              listen 80 default_server;
              listen [::]:80 default_server;
              root /var/www/html;
              index index.html;
              server_name _;
              location / {
                  try_files $uri $uri/ =404;
              }
          }
        dest: /etc/nginx/sites-enabled/app
        mode: '0644'
      notify: restart nginx

    - name: Ensure nginx is started and enabled
      service:
        name: nginx
        state: started
        enabled: yes

  handlers:
    - name: restart nginx
      service:
        name: nginx
        state: restarted
```

## Pipeline health check
```bash
curl -f --retry 10 --retry-delay 5 --retry-connrefused http://SERVER_IP
```

## Terraform ports
- 22 (SSH)
- 80 (HTTP)
- 443 (HTTPS optional)

## Notes
- HTML goes in /var/www/html/index.html
- Config goes in /etc/nginx/sites-enabled/app
- Always remove default config to avoid conflicts
- Use www-data as owner for web files
