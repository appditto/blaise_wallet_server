# Blaise (Pascal)

## What is Blaise and Pascal?

Blaise is a mobile wallet written with Flutter. Pascal is a cryptocurrency.

| Link | Description |
| :----- | :------ |
[pascalcoin.org](https://pascalcoin.org) | Pascal Homepage
[blaisewallet.com](https://blaisewallet.com) | Blaise Home Page
[appditto.com](https://appditto.com) | Appditto Homepage

## Requirements

**Requires Python 3.6 or Newer**

Install requirements on Ubuntu 18.04:
```
apt install python3 python3-dev libdpkg-perl virtualenv nginx
```

Minimum of one **PascalCoin Daemon** with RPC enabled.

**Redis server** running on the default port 6379

On debian-based systems redis can be installed with:

```
sudo apt install redis-server
```

## Installation

Generally:

1) Run the app under a dedicated user
2) Clone the repository
3) Configuration
4) Run

```
sudo adduser blaise # Add natriumuser
sudo usermod -aG sudo blaise # Add natriumuser to sudo group
sudo usermod -aG www-data blaise # Add natriumuser to www-data group
sudo su - blaise # Change to natriumuser
git clone https://github.com/appditto/blaise_wallet_server.git blaise_server # Clone repository
```

Ensure python3.6 or newer is installed (`python3 --version`) and

```
cd blaise_server
virtualenv -p python3 venv
source venv/bin/activate
pip install -r requirements.txt
```

Some things can be configured with environment variables, most are optional

Create the file `.env` in the same directory as `blaise_server.py` with the contents:

```
RPC_URL=http://127.0.0.1:4003  # Pascal RPC URL
DEBUG=0                   # Debug mode (0 is off)
FCM_API_KEY=None          # (Optional) Firebase Legacy API KEY (From Firebase Console)
FCM_SENDER_ID=1234        # (Optional) Firebase Sender ID (From Firebase Console)
SIGNER_ACCOUNT=1234       # (Optional) Account on the wallet that is used to sign transactions related to the borrow process
PUBKEY_B58=3gb0p          # (Optional) Public key of the daemon wallet
```

## Running

The recommended configuration is to run the server behind [nginx](https://www.nginx.com/), which will act as a reverse proxy

Next, we'll define a systemd service unit

/etc/systemd/system/blaise@.service
```
[Unit]
Description=Blaise Server
After=network.target

[Service]
Type=simple
User=blaise
Group=www-data
EnvironmentFile=/home/blaise/blaise_server/.env
WorkingDirectory=/home/blaise/blaise_server
ExecStart=/home/blaise/blaise_server/venv/bin/python natriumcast.py --host 127.0.0.1 --port %i --log-file /tmp/blaise%i.log
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Enable this service and start it, ensure all is working as expected

```
sudo systemctl enable blaise@4443
sudo systemctl start natriumcast@4443
sudo systemctl status natriumcast@4443
```

Next, configure nginx to proxy requests to this server

/etc/nginx/sites-available/blaiseapi.appditto.com

```
upstream blaise_nodes {
        least_conn;

        server 127.0.0.1:4443;
}

server {
        server_name blaiseapi.appditto.com;

        location / {
                proxy_pass http://blaise_nodes;
                proxy_redirect off;
                proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
                proxy_set_header X-Real-IP $remote_addr;
                proxy_set_header X-Forwarded-Host $server_name;
                proxy_set_header X-Forwarded-Proto $scheme;
                proxy_http_version 1.1;
                proxy_set_header Host $host;
                proxy_set_header Upgrade $http_upgrade;
                proxy_set_header Connection "upgrade";
        }
}

```

Enable this configuration and restart nginx

```
sudo ln -s /etc/nginx/sites-available/blaiseapi.appditto.com /etc/nginx/sites-enabled/blaiseapi.appditto.com
sudo service nginx restart
```

## Let's encrypt

```
sudo add-apt-repository ppa:certbot/certbot
sudo apt update
sudo apt install python-certbot-nginx 
sudo certbot --nginx
```
