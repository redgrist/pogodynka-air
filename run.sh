docker run -d \
  --name pogodynka \
  -p 5000:5000 \
  --device=/dev/serial0:/dev/serial0 \
  --device=/dev/i2c-1:/dev/i2c-1 \
  --restart unless-stopped \
  air-dashboard 
