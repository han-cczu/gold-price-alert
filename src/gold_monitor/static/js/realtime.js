/** One socket and one retry/heartbeat timer per page, with explicit disposal. */
export function createRealtime({ onMessage, onStatus = () => {}, WebSocketImpl = globalThis.WebSocket, location = globalThis.location, retryMs = 3000, heartbeatMs = 30000 }) {
    let socket = null;
    let retry = null;
    let heartbeat = null;
    let stopped = true;
    let connected = false;
    let awaitingPong = false;
    function clearHeartbeat() { clearInterval(heartbeat); heartbeat = null; }
    function connect() {
        if (stopped || (socket && socket.readyState < 2)) return;
        const current = new WebSocketImpl(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws`);
        socket = current;
        current.onopen = () => {
            if (stopped || current !== socket) return;
            connected = true;
            awaitingPong = false;
            onStatus(true);
            clearTimeout(retry);
            clearHeartbeat();
            heartbeat = setInterval(() => {
                if (awaitingPong) { current.close(); return; }
                if (current.readyState === 1) { awaitingPong = true; current.send(JSON.stringify({ type: 'ping' })); }
            }, heartbeatMs);
        };
        current.onmessage = event => {
            if (current !== socket || stopped) return;
            try {
                const message = JSON.parse(event.data);
                if (!message || typeof message !== 'object') return;
                if (message.type === 'pong') awaitingPong = false;
                else onMessage(message);
            } catch { /* Ignore malformed frames; later valid messages remain usable. */ }
        };
        current.onclose = () => {
            if (current !== socket) return;
            connected = false;
            clearHeartbeat();
            onStatus(false);
            if (!stopped) retry = setTimeout(connect, retryMs);
        };
        current.onerror = () => current.close();
    }
    return {
        start() { if (!stopped) return; stopped = false; connect(); },
        stop() { stopped = true; clearTimeout(retry); clearHeartbeat(); connected = false; socket?.close(); socket = null; },
        get connected() { return connected; },
    };
}
