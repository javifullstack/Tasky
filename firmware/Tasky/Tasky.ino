#include <Arduino.h>
#include <ArduinoJson.h>
#include <atomic>
#include <FT6336U.h>
#include <HTTPClient.h>
#include <TFT_eSPI.h>
#include <WiFi.h>
#include <vector>

#include "secrets.h"

namespace {

constexpr uint8_t kTouchSda = 16;
constexpr uint8_t kTouchScl = 15;
constexpr uint8_t kTouchReset = 18;
constexpr uint8_t kTouchInterrupt = 17;
constexpr uint8_t kPageSize = 5;
constexpr int16_t kScreenWidth = 480;
constexpr int16_t kScreenHeight = 320;

constexpr uint16_t kBackground = 0x0862;
constexpr uint16_t kSurface = 0x10C5;
constexpr uint16_t kPanelActive = 0x1928;
constexpr uint16_t kPanelBorder = 0x2DAA;
constexpr uint16_t kMuted = 0x8D38;
constexpr uint16_t kAccent = 0x271F;
constexpr uint16_t kMagenta = 0xFA1A;
constexpr uint16_t kTodoistColor = 0xE226;
constexpr uint16_t kTrelloColor = 0x033F;
constexpr uint16_t kGood = 0x6FF6;
constexpr uint16_t kBad = 0xFA0F;

struct TaskItem {
  String id;
  String title;
  String due;
  String context;
  String url;
};

enum class CommandType : uint8_t { Refresh, LoadPage, Complete, Archive };

struct NetworkCommand {
  CommandType type;
  char id[65];
};

TFT_eSPI display;
FT6336U touch(kTouchSda, kTouchScl, kTouchReset, kTouchInterrupt);
QueueHandle_t commandQueue;
SemaphoreHandle_t stateMutex;
std::vector<TaskItem> visibleTasks;
String currentProvider = "todoist";
String providerStatus = "CONECTANDO";
String updatedAt;
uint32_t totalTasks = 0;
uint32_t pageIndex = 0;
uint32_t refreshIntervalMs = 300000;
int selectedTask = -1;
bool confirmationOpen = false;
bool refreshing = false;
bool touchedLastLoop = false;
bool touchInitialized = false;
std::atomic_bool uiDirty{true};

String endpoint(const String& path) {
  String base = TASKY_SERVICE_URL;
  while (base.endsWith("/")) base.remove(base.length() - 1);
  return base + path;
}

int authorizedRequest(HTTPClient& client, const String& url, int method) {
  WiFiClient transport;
  client.setTimeout(8000);
  if (!client.begin(transport, url)) return -1;
  client.addHeader("Authorization", String("Bearer ") + TASKY_DEVICE_TOKEN);
  int status = method == HTTP_GET ? client.GET() : client.POST("");
  return status;
}

void setStatus(const String& status, bool isRefreshing) {
  xSemaphoreTake(stateMutex, portMAX_DELAY);
  providerStatus = status;
  refreshing = isRefreshing;
  uiDirty = true;
  xSemaphoreGive(stateMutex);
}

bool fetchPage(bool force) {
  String provider;
  uint32_t offset;
  xSemaphoreTake(stateMutex, portMAX_DELAY);
  provider = currentProvider;
  offset = pageIndex * kPageSize;
  xSemaphoreGive(stateMutex);

  String path = "/api/v1/today?source=" + provider + "&offset=" + String(offset) + "&limit=" + String(kPageSize);
  if (force) path += "&refresh=1";
  HTTPClient client;
  int responseCode = authorizedRequest(client, endpoint(path), HTTP_GET);
  if (responseCode < 200 || responseCode >= 300) {
    client.end();
    setStatus(responseCode < 0 ? "SIN CONEXION AL SERVICIO" : "ERROR DEL SERVICIO", false);
    return false;
  }

  String payload = client.getString();
  client.end();
  JsonDocument document;
  if (deserializeJson(document, payload)) {
    setStatus("RESPUESTA INVALIDA", false);
    return false;
  }

  JsonObject providerData = document["providers"][provider].as<JsonObject>();
  std::vector<TaskItem> newTasks;
  for (JsonObject item : providerData["tasks"].as<JsonArray>()) {
    TaskItem taskItem;
    taskItem.id = item["id"] | "";
    taskItem.title = item["title"] | "(sin titulo)";
    taskItem.due = item["due"] | "";
    taskItem.context = item["context"] | "";
    taskItem.url = item["url"] | "";
    newTasks.push_back(taskItem);
  }

  String status = providerData["status"] | "error";
  String nextUpdatedAt = providerData["updated_at"] | "";
  uint32_t nextRefreshInterval = document["refresh_interval_seconds"] | 300;
  xSemaphoreTake(stateMutex, portMAX_DELAY);
  visibleTasks = newTasks;
  totalTasks = providerData["total"] | 0;
  providerStatus = status;
  updatedAt = nextUpdatedAt;
  refreshIntervalMs = max(60UL, nextRefreshInterval) * 1000UL;
  refreshing = false;
  selectedTask = -1;
  uiDirty = true;
  xSemaphoreGive(stateMutex);
  return status == "ok" || status == "stale";
}

bool postAction(CommandType type, const String& id) {
  const String actionPath = type == CommandType::Complete ? "/complete" : "/archive";
  const String resource = type == CommandType::Complete ? "tasks/" : "cards/";
  HTTPClient client;
  int responseCode = authorizedRequest(client, endpoint("/api/v1/" + resource + id + actionPath), HTTP_POST);
  client.end();
  return responseCode >= 200 && responseCode < 300;
}

void networkWorker(void*) {
  WiFi.mode(WIFI_STA);
  WiFi.begin(TASKY_WIFI_SSID, TASKY_WIFI_PASSWORD);
  uint32_t lastRefresh = 0;
  NetworkCommand command{};
  for (;;) {
    if (WiFi.status() != WL_CONNECTED) {
      setStatus("WIFI DESCONECTADO", false);
      WiFi.reconnect();
      vTaskDelay(pdMS_TO_TICKS(1000));
      continue;
    }

    if (xQueueReceive(commandQueue, &command, pdMS_TO_TICKS(1000)) == pdTRUE) {
      if (command.type == CommandType::Complete || command.type == CommandType::Archive) {
        setStatus("GUARDANDO CAMBIO", true);
        bool actionSucceeded = postAction(command.type, String(command.id));
        fetchPage(false);
        if (!actionSucceeded) setStatus("NO SE PUDO GUARDAR", false);
      } else if (command.type == CommandType::Refresh) {
        setStatus("ACTUALIZANDO", true);
        fetchPage(true);
        lastRefresh = millis();
      } else {
        fetchPage(false);
      }
    } else if (lastRefresh == 0 || millis() - lastRefresh >= refreshIntervalMs) {
      setStatus("ACTUALIZANDO", true);
      fetchPage(lastRefresh == 0);
      lastRefresh = millis();
    }
  }
}

String clipped(String value, size_t maxLength) {
  if (value.length() <= maxLength) return value;
  value.remove(maxLength - 1);
  value += "~";
  return value;
}

String shortTime(const String& value) {
  if (value.length() >= 16 && value[10] == 'T') return value.substring(11, 16);
  if (value.length() >= 10) return value.substring(5, 10);
  return value;
}

void drawButton(int16_t x, int16_t y, int16_t width, int16_t height, const String& label, uint16_t color) {
  display.fillRoundRect(x, y, width, height, 6, color);
  uint16_t textColor = (color == kSurface || color == kPanelBorder || color == kTrelloColor) ? TFT_WHITE : kBackground;
  display.setTextColor(textColor, color);
  display.setTextDatum(MC_DATUM);
  display.drawString(label, x + width / 2, y + height / 2, 2);
  display.setTextDatum(TL_DATUM);
}

void drawTodoistMark(int16_t x, int16_t y) {
  display.fillRoundRect(x, y + 2, 18, 4, 2, kTodoistColor);
  display.fillRoundRect(x + 4, y + 9, 14, 4, 2, kTodoistColor);
  display.fillRoundRect(x + 8, y + 16, 10, 4, 2, kTodoistColor);
}

void drawTaskyMark(int16_t x, int16_t y) {
  display.drawRoundRect(x, y, 22, 22, 4, kAccent);
  display.drawLine(x + 15, y + 1, x + 20, y + 6, kAccent);
  display.drawLine(x + 7, y + 12, x + 10, y + 15, kMagenta);
  display.drawLine(x + 10, y + 15, x + 17, y + 7, kMagenta);
}

void drawTrelloMark(int16_t x, int16_t y) {
  display.fillRoundRect(x, y, 22, 22, 4, kTrelloColor);
  display.fillRoundRect(x + 5, y + 4, 5, 14, 1, TFT_WHITE);
  display.fillRoundRect(x + 13, y + 4, 5, 9, 1, TFT_WHITE);
}

void drawSourceButton(int16_t x, int16_t y, bool active, bool todoist) {
  const uint16_t brandColor = todoist ? kTodoistColor : kTrelloColor;
  display.fillRoundRect(x, y, 222, 34, 6, active ? kPanelActive : kSurface);
  display.drawRoundRect(x, y, 222, 34, 6, active ? kAccent : kPanelBorder);
  if (todoist) drawTodoistMark(x + 67, y + 6);
  else drawTrelloMark(x + 67, y + 6);
  display.setTextColor(TFT_WHITE, active ? kPanelActive : kSurface);
  display.drawString(todoist ? "Todoist" : "Trello", x + 96, y + 10, 2);
  display.drawFastHLine(x + 67, y + 31, 22, brandColor);
}

void drawScreen() {
  std::vector<TaskItem> tasks;
  String provider;
  String status;
  String refreshed;
  uint32_t total;
  uint32_t page;
  int selected;
  bool confirming;
  bool loading;

  xSemaphoreTake(stateMutex, portMAX_DELAY);
  tasks = visibleTasks;
  provider = currentProvider;
  status = providerStatus;
  refreshed = updatedAt;
  total = totalTasks;
  page = pageIndex;
  selected = selectedTask;
  confirming = confirmationOpen;
  loading = refreshing;
  uiDirty = false;
  xSemaphoreGive(stateMutex);

  display.fillScreen(kBackground);
  display.fillRect(0, 0, kScreenWidth, 44, kSurface);
  display.setTextColor(TFT_WHITE, kSurface);
  drawTaskyMark(16, 10);
  display.drawString("TASKY", 45, 11, 4);
  display.setTextColor(kMuted, kSurface);
  display.drawString("PENDIENTES DE HOY", 130, 15, 2);
  drawButton(397, 6, 70, 32, loading ? "..." : "REFRESCAR", kAccent);

  drawSourceButton(14, 50, provider == "todoist", true);
  drawSourceButton(244, 50, provider == "trello", false);

  if (tasks.empty()) {
    display.setTextColor(TFT_WHITE, kBackground);
    display.drawCentreString(loading ? "CARGANDO" : "SIN TAREAS VENCIDAS O DE HOY", 240, 150, 2);
  }

  for (size_t index = 0; index < tasks.size() && index < kPageSize; ++index) {
    const int16_t y = 91 + static_cast<int16_t>(index) * 34;
    const bool isSelected = selected == static_cast<int>(index);
    display.fillRoundRect(12, y, 456, 30, 5, isSelected ? kPanelActive : kSurface);
    display.fillRect(12, y + 3, 4, 24, kAccent);
    display.setTextColor(TFT_WHITE, isSelected ? kPanelActive : kSurface);
    display.drawString(clipped(tasks[index].title, 42), 24, y + 1, 2);
    display.setTextColor(kMuted, isSelected ? kPanelActive : kSurface);
    display.drawString(clipped(tasks[index].context, 30), 24, y + 17, 1);
    display.setTextColor(kAccent, isSelected ? kPanelActive : kSurface);
    display.setTextDatum(TR_DATUM);
    display.drawString(shortTime(tasks[index].due), 459, y + 7, 2);
    display.setTextDatum(TL_DATUM);
  }

  display.drawFastHLine(12, 267, 456, kPanelBorder);
  String providerLabel = provider == "todoist" ? "Todoist" : "Trello";
  String statusLabel = status == "ok" ? "ACTUALIZADO" :
                       status == "stale" ? "SIN RED · DATOS GUARDADOS" :
                       status == "not_configured" ? "SIN CONFIGURAR" :
                       status == "error" ? "SIN CONEXION" : status;
  display.setTextColor(status == "ok" ? kGood : (status == "stale" ? kAccent : kBad), kBackground);
  display.drawString(clipped(providerLabel + " · " + statusLabel, 37), 14, 273, 1);
  display.setTextColor(kMuted, kBackground);
  display.setTextDatum(TR_DATUM);
  display.drawString(refreshed.length() >= 16 ? "ACT " + refreshed.substring(11, 16) : "ACT --:--", 466, 273, 1);
  display.setTextDatum(TL_DATUM);

  drawButton(14, 291, 66, 25, "<", page > 0 ? kSurface : kPanelBorder);
  drawButton(87, 291, 66, 25, ">", (page + 1) * kPageSize < total ? kSurface : kPanelBorder);
  String pageText = total == 0 ? "0 tareas" : String(page * kPageSize + 1) + "-" + String(min((page + 1) * kPageSize, total)) + " / " + String(total);
  display.setTextColor(kMuted, kBackground);
  display.setTextDatum(MC_DATUM);
  display.drawString(pageText, 238, 303, 1);
  display.setTextDatum(TL_DATUM);

  if (selected >= 0 && selected < static_cast<int>(tasks.size())) {
    drawButton(330, 289, 138, 29, provider == "todoist" ? "COMPLETAR" : "ARCHIVAR", provider == "todoist" ? kGood : kTrelloColor);
  }

  if (confirming) {
    display.fillRect(0, 0, kScreenWidth, kScreenHeight, 0x0000);
    display.drawRoundRect(28, 91, 424, 138, 8, kMagenta);
    display.setTextColor(TFT_WHITE, TFT_BLACK);
    display.drawCentreString(provider == "todoist" ? "Completar esta tarea?" : "Archivar esta tarjeta?", 240, 120, 4);
    drawButton(50, 173, 165, 38, "CANCELAR", kSurface);
    drawButton(265, 173, 165, 38, "CONFIRMAR", provider == "todoist" ? kGood : kTrelloColor);
  }
}

void queueCommand(CommandType type, const String& id = "") {
  NetworkCommand command{};
  command.type = type;
  id.substring(0, sizeof(command.id) - 1).toCharArray(command.id, sizeof(command.id));
  xQueueSend(commandQueue, &command, 0);
}

void handleTouch(int16_t x, int16_t y) {
  if (confirmationOpen) {
    if (y >= 165 && y <= 220) {
      if (x < 240) {
        confirmationOpen = false;
      } else {
        TaskItem item;
        bool hasItem = false;
        xSemaphoreTake(stateMutex, portMAX_DELAY);
        if (selectedTask >= 0 && selectedTask < static_cast<int>(visibleTasks.size())) {
          item = visibleTasks[selectedTask];
          hasItem = true;
        }
        xSemaphoreGive(stateMutex);
        if (hasItem) queueCommand(currentProvider == "todoist" ? CommandType::Complete : CommandType::Archive, item.id);
        confirmationOpen = false;
      }
      uiDirty = true;
    }
    return;
  }

  if (y < 43 && x >= 380) {
    queueCommand(CommandType::Refresh);
    return;
  }
  if (y >= 50 && y < 87) {
    xSemaphoreTake(stateMutex, portMAX_DELAY);
    currentProvider = x < 240 ? "todoist" : "trello";
    pageIndex = 0;
    selectedTask = -1;
    uiDirty = true;
    xSemaphoreGive(stateMutex);
    queueCommand(CommandType::LoadPage);
    return;
  }
  if (y >= 91 && y < 261) {
    int index = (y - 91) / 34;
    xSemaphoreTake(stateMutex, portMAX_DELAY);
    if (index >= 0 && index < static_cast<int>(visibleTasks.size())) selectedTask = index;
    uiDirty = true;
    xSemaphoreGive(stateMutex);
    return;
  }
  if (y >= 288 && x < 160) {
    xSemaphoreTake(stateMutex, portMAX_DELAY);
    if (x < 82 && pageIndex > 0) --pageIndex;
    else if (x >= 82 && (pageIndex + 1) * kPageSize < totalTasks) ++pageIndex;
    selectedTask = -1;
    uiDirty = true;
    xSemaphoreGive(stateMutex);
    queueCommand(CommandType::LoadPage);
    return;
  }
  if (y >= 285 && x >= 320) {
    xSemaphoreTake(stateMutex, portMAX_DELAY);
    bool hasSelection = selectedTask >= 0 && selectedTask < static_cast<int>(visibleTasks.size());
    xSemaphoreGive(stateMutex);
    if (hasSelection) confirmationOpen = true;
    uiDirty = true;
  }
}

}  // namespace

void setup() {
  Serial.begin(115200);
  display.init();
  display.setRotation(1);
  display.setTextWrap(false);
  display.fillScreen(kBackground);
  display.setTextColor(TFT_WHITE, kBackground);
  display.drawCentreString("TASKY", kScreenWidth / 2, 130, 4);

  touch.begin();
  touchInitialized = true;
  stateMutex = xSemaphoreCreateMutex();
  commandQueue = xQueueCreate(4, sizeof(NetworkCommand));
  if (stateMutex == nullptr || commandQueue == nullptr) {
    display.drawCentreString("ERROR DE MEMORIA", kScreenWidth / 2, 180, 2);
    for (;;) delay(1000);
  }

  xTaskCreatePinnedToCore(networkWorker, "tasky-network", 12288, nullptr, 1, nullptr, 0);
}

void loop() {
  if (touchInitialized) {
    FT6336U_TouchPointType point = touch.scan();
    bool down = point.touch_count > 0;
    if (down && !touchedLastLoop) handleTouch(point.tp[0].x, point.tp[0].y);
    touchedLastLoop = down;
  }

  if (uiDirty) drawScreen();
  vTaskDelay(pdMS_TO_TICKS(25));
}