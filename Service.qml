import QtQuick
import Quickshell
import Quickshell.Io

Item {
  id: root

  property var shell: null
  property string phase: "starting"
  property string detail: "Starting Tailbridge"
  property string baseUrl: ""
  property bool configured: false
  property var qrRows: []
  property string qrKind: ""
  property string qrItemKind: ""
  property string qrItemName: ""
  property string qrError: ""
  property bool qrBusy: false
  property string qrRequestId: ""
  property int requestSerial: 0
  property bool stopping: false
  property bool fatalSeen: false

  readonly property bool ready: phase === "ready"
  readonly property int qrSize: qrRows.length
  readonly property string bridgePath: decodeURIComponent(Qt.resolvedUrl("bridge.py").toString().replace(/^file:\/\//, ""))

  function startBackend() {
    if (backend.running || stopping) return
    fatalSeen = false
    phase = "starting"
    detail = "Starting Tailbridge"
    backend.command = ["setpriv", "--pdeathsig=TERM", "python3", bridgePath]
    backend.running = true
  }

  function send(action) {
    if (!backend.running) {
      qrError = "Tailbridge service is not running"
      return ""
    }
    requestSerial += 1
    var requestId = String(requestSerial)
    backend.write(JSON.stringify({ id: requestId, action: action }) + "\n")
    return requestId
  }

  function requestQr(action) {
    qrError = ""
    if (!ready) {
      qrError = "Tailbridge service is not ready"
      return
    }
    qrRequestId = send(action)
    qrBusy = qrRequestId !== ""
    if (qrBusy) requestTimer.restart()
  }

  function requestSetup() {
    requestQr("setup")
  }

  function requestInstall() {
    requestQr("install")
  }

  function requestClipboard() {
    requestQr("claim")
  }

  function completeSetup() {
    qrError = ""
    qrRequestId = send("configured")
    qrBusy = qrRequestId !== ""
    if (qrBusy) requestTimer.restart()
  }

  function dismissQr() {
    var hadClaim = qrKind === "claim"
    clearQr()
    if (hadClaim) send("clear")
  }

  function clearQr() {
    requestTimer.stop()
    qrRows = []
    qrKind = ""
    qrItemKind = ""
    qrItemName = ""
    qrError = ""
    qrBusy = false
    qrRequestId = ""
  }

  function handleLine(line) {
    var message
    try {
      message = JSON.parse(String(line || ""))
    } catch (error) {
      console.warn("tailbridge: ignored malformed service output")
      return
    }
    if (message.event === "ready") {
      baseUrl = String(message.baseUrl || "")
      phase = String(message.status || "inactive")
      detail = String(message.detail || "")
      configured = message.configured === true
      return
    }
    if (message.event === "fatal") {
      fatalSeen = true
      phase = "error"
      detail = String(message.error || "Tailbridge failed to start")
      return
    }
    if (message.event === "status") {
      phase = String(message.status || "error")
      detail = String(message.detail || "")
      return
    }
    if (message.id !== undefined) {
      var responseId = String(message.id)
      if (message.ok !== true) {
        if (responseId !== qrRequestId) return
        requestTimer.stop()
        qrBusy = false
        qrRequestId = ""
        qrError = String(message.error || "Tailbridge request failed")
        return
      }
      if (Array.isArray(message.rows)) {
        if (responseId !== qrRequestId) return
        requestTimer.stop()
        qrKind = String(message.kind || "")
        qrItemKind = String(message.itemKind || "")
        qrItemName = String(message.itemName || "")
        qrRows = message.rows
        qrBusy = false
        qrRequestId = ""
        qrError = ""
      }
      if (message.configured === true) {
        if (responseId !== qrRequestId) return
        configured = true
        clearQr()
      }
    }
  }

  Process {
    id: backend
    stdinEnabled: true
    stdout: SplitParser { onRead: function(line) { root.handleLine(line) } }
    stderr: SplitParser { onRead: function(line) { console.warn("tailbridge:", String(line || "")) } }
    onExited: function(exitCode) {
      if (root.stopping) return
      root.clearQr()
      root.baseUrl = ""
      root.phase = "error"
      if (!root.fatalSeen) root.detail = "Tailbridge service stopped"
      restartTimer.restart()
    }
  }

  Timer {
    id: restartTimer
    interval: 3000
    repeat: false
    onTriggered: root.startBackend()
  }

  Timer {
    id: requestTimer
    interval: 15000
    repeat: false
    onTriggered: {
      root.qrBusy = false
      root.qrRequestId = ""
      root.qrError = "Tailbridge did not respond; try again"
    }
  }

  Component.onCompleted: startBackend()
  Component.onDestruction: {
    stopping = true
    restartTimer.stop()
    if (backend.running) backend.running = false
  }
}
