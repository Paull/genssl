#!/usr/bin/env bash
set -e
cd "$(dirname "${BASH_SOURCE[0]}")"

if [ -f "out/rsa_root.crt" ] || [ -f "out/ec_root.crt" ]; then
  echo Root certificate already exists.
  exit 1
fi

if [ -z "${rootpass:-}" ]; then
  echo Root pass is not provided.
  exit 1
fi

if [ ! -d "out/newcerts" ] || [ ! -f "out/index.txt" ] || [ ! -f "out/serial" ]; then
  bash flush.sh
fi

# 生成EC私钥
openssl ecparam -genkey -name prime256v1 | openssl ec -aes256 -out out/ec_root.key -passout env:rootpass
# 生成EC根证书
openssl req -new -x509 -config ca.cnf \
  -key out/ec_root.key \
  -out out/ec_root.crt \
  -subj "/C=CN/O=Corn Technology./CN=Corn Root CA" \
  -passin env:rootpass -days 7300

# 查看EC证书信息，确认包含 CA:TRUE
openssl x509 -text -noout -in out/ec_root.crt

# Generate root cert along with root key
openssl req -new -x509 -config ca.cnf \
  -newkey rsa:2048 -keyout out/rsa_root.key \
  -out out/rsa_root.crt \
  -subj "/C=CN/O=Corn Technology./CN=Corn Root CA" \
  -passout env:rootpass -days 7300

# 查看证书信息，确认包含 CA:TRUE
openssl x509 -text -noout -in out/rsa_root.crt
