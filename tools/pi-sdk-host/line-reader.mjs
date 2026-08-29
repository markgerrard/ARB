// Persistent newline framing for a stream socket. A single data event may
// contain a fragment, one complete line, or several complete lines plus a
// fragment; this reader preserves all bytes not consumed by the current read.

export function createLineReader(socket) {
  let buffer = "";
  let closed = false;
  let failure = null;
  const waiters = [];

  function rejectWaiters(error) {
    while (waiters.length > 0) waiters.shift().reject(error);
  }

  function drain() {
    while (waiters.length > 0) {
      const newline = buffer.indexOf("\n");
      if (newline < 0) break;
      const line = buffer.slice(0, newline);
      buffer = buffer.slice(newline + 1);
      waiters.shift().resolve(line);
    }
    if (closed && waiters.length > 0) {
      rejectWaiters(failure ?? new Error("socket closed before newline"));
    }
  }

  const onData = (chunk) => {
    buffer += chunk.toString();
    drain();
  };
  const onError = (error) => {
    failure = error;
    closed = true;
    rejectWaiters(error);
  };
  const onClose = () => {
    closed = true;
    drain();
  };
  socket.on("data", onData);
  socket.on("error", onError);
  socket.on("close", onClose);

  return {
    readLine() {
      const newline = buffer.indexOf("\n");
      if (newline >= 0) {
        const line = buffer.slice(0, newline);
        buffer = buffer.slice(newline + 1);
        return Promise.resolve(line);
      }
      if (closed) return Promise.reject(failure ?? new Error("socket closed before newline"));
      return new Promise((resolve, reject) => waiters.push({ resolve, reject }));
    },
    close() {
      socket.off("data", onData);
      socket.off("error", onError);
      socket.off("close", onClose);
      closed = true;
      rejectWaiters(failure ?? new Error("line reader closed"));
    },
  };
}
