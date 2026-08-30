import QtQuick
import qs.Commons
import qs.Ui

BarWidget {
  id: root
  moduleName: "swheel33.tailbridge"

  readonly property var bridge: bar?.shell?.serviceFor(moduleName)
  readonly property bool connected: bridge && bridge.ready
  readonly property bool opened: panelLoader.item ? panelLoader.item.opened === true : false

  function injectPanel() {
    var panel = panelLoader.item
    if (!panel) return
    panel.bar = root.bar
    panel.anchorItem = button
    panel.hostWidget = root
    panel.service = root.bridge
  }

  function open() {
    injectPanel()
    if (panelLoader.item) panelLoader.item.open()
  }

  function close() {
    if (panelLoader.item) panelLoader.item.close()
  }

  function toggle() {
    if (opened) close()
    else open()
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onBarChanged: injectPanel()
  onBridgeChanged: injectPanel()

  Loader {
    id: panelLoader
    active: true
    source: Qt.resolvedUrl("Panel.qml")
    visible: false
    onLoaded: {
      root.injectPanel()
      Qt.callLater(root.injectPanel)
    }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.bridge && root.bridge.configured ? "󰅍" : "󰨸"
    onPressed: function(mouseButton) {
      if (mouseButton === Qt.LeftButton) root.toggle()
    }
  }
}
