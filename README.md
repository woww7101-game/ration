# RadioWave 3D — Render starter

## Deploy
1. Create a MongoDB Atlas cluster and database user. Copy its connection string.
2. Upload this folder to a GitHub repository.
3. In Render, create a Web Service from the repository. Build command: `pip install -r requirements.txt`; start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`.
4. Set environment variables:
   - `MONGODB_URI` = MongoDB Atlas connection URI
   - `MONGODB_DB` = `radiowave3d`
   - `SECRET_KEY` = a long random secret
   - `COOKIE_SECURE` = `true`
5. Deploy and open `/health`; expect `mongodb_configured` and `mongodb_connected` to be true.

## Important
- This is a prototype/starter, not a load-tested production voice platform.
- Audio is streamed with browser WebRTC peer-to-peer; FastAPI WebSocket handles signaling and virtual channel membership. Peer-to-peer mesh is intended for small groups. For larger groups use a WebRTC SFU such as LiveKit.
- STUN is configured in the browser. Some networks require a TURN server for reliable connectivity; add your own TURN credentials in `static/app.js`.
- The frequency is a virtual internet room, not a real RF transmission. Audio is not stored.
- Microphone access requires HTTPS (or localhost). For local HTTP development, set `COOKIE_SECURE=false`.
