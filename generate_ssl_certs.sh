#!/bin/bash

# Generate a private key
openssl genrsa -out server.key 2048

# Generate a Certificate Signing Request (CSR)
openssl req -new -key server.key -out server.csr -subj "/C=US/ST=State/L=City/O=Organization/OU=Org Unit/CN=localhost"

# Generate a self-signed certificate
openssl x509 -req -days 365 -in server.csr -signkey server.key -out server.crt

# Remove the CSR as it's no longer needed
rm server.csr

echo "SSL certificate generation complete. Created server.key and server.crt"