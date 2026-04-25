#include <Arduino.h>
#include <WiFi.h>
#include <WiFiUdp.h>

#include "../include/secrets.h"

// ============================================================
// Konfiguracja kompilacji / debugowania
// ============================================================
// Ustaw na 0, gdy chcesz maksymalnie odchudzić logi przez UART.
#define ENABLE_DEBUG_LOGS 1

#if ENABLE_DEBUG_LOGS
#define DBG_PRINTF(...) Serial.printf(__VA_ARGS__)
#define DBG_PRINTLN(...) Serial.println(__VA_ARGS__)
#define DBG_PRINT(...) Serial.print(__VA_ARGS__)
#define DBG_BEGIN(...) Serial.begin(__VA_ARGS__)
#else
#define DBG_PRINTF(...)
#define DBG_PRINTLN(...)
#define DBG_PRINT(...)
#define DBG_BEGIN(...)
#endif

// ============================================================
// Konfiguracja sieci
// ============================================================
// Dane logowania są dostarczane z lokalnego pliku include/secrets.h,
// który jest ignorowany przez git.
const char *ssid = WIFI_SSID;
const char *password = WIFI_PASSWORD;

// Port UDP, na ktorym ESP32 nasluchuje komend sterujacych.
constexpr uint16_t localUdpPort = 4210;

WiFiUDP udp;

// ============================================================
// Definicje pinow (przykladowe dla L298N / TB6612)
// ============================================================
// PWM dla napedu i skretu.
constexpr uint8_t PIN_PWM_DRIVE = 15;
constexpr uint8_t PIN_PWM_STEER = 16;

// Kierunek mostka H dla napedu i skretu.
constexpr uint8_t PIN_DIR_DRIVE_A = 4;
constexpr uint8_t PIN_DIR_DRIVE_B = 5;
constexpr uint8_t PIN_DIR_STEER_A = 6;
constexpr uint8_t PIN_DIR_STEER_B = 7;

// Bity maski w polu motor_flags (czytelniejsze od "surowych" binarek).
constexpr uint8_t FLAG_DRIVE_A = 0b00000001;
constexpr uint8_t FLAG_DRIVE_B = 0b00000010;
constexpr uint8_t FLAG_STEER_A = 0b00000100;
constexpr uint8_t FLAG_STEER_B = 0b00001000;

// ============================================================
// Struktura danych przesylana po UDP (Zero-Copy payload)
// ============================================================
// "packed" wymusza brak wypelniania (padding), dzieki czemu
// rozmiar ramki jest zawsze taki sam po obu stronach.
struct __attribute__((packed)) RC_Command
{
  uint16_t seq_id;     // 2 bajty: numer sekwencyjny anty-duplikatowy
  uint8_t pwm_drive;   // 1 bajt: wypelnienie PWM napedu (0..255)
  uint8_t pwm_steer;   // 1 bajt: wypelnienie PWM skretu (0..255)
  uint8_t motor_flags; // 1 bajt: bity kierunku mostka H
}; // Razem: 5 bajtow

// Kolejka FreeRTOS do bezpiecznej wymiany danych miedzy taskami.
// Rozmiar = 1, bo interesuje nas tylko najnowsza komenda sterujaca.
QueueHandle_t rcCommandQueue = nullptr;

// ============================================================
// Funkcje pomocnicze
// ============================================================

// Porownanie numerow sekwencyjnych odporne na overflow uint16_t.
// Gdy seq_id przeleci z 0xFFFF na 0, logika nadal dziala poprawnie.
static inline bool isSeqNewer(uint16_t candidate, uint16_t reference)
{
  return static_cast<int16_t>(candidate - reference) > 0;
}

// Wspolna funkcja "twardego" zatrzymania napedu.
// Uzywana przy starcie i w failsafe.
static inline void stopMotorsSafe()
{
  analogWrite(PIN_PWM_DRIVE, 0);
  analogWrite(PIN_PWM_STEER, 0);

  digitalWrite(PIN_DIR_DRIVE_A, LOW);
  digitalWrite(PIN_DIR_DRIVE_B, LOW);
  digitalWrite(PIN_DIR_STEER_A, LOW);
  digitalWrite(PIN_DIR_STEER_B, LOW);
}

// Zastosowanie otrzymanej komendy do wyjsc GPIO.
static inline void applyMotorCommand(const RC_Command &cmd)
{
  // 1) Kierunek mostka H z mapowania bitow.
  digitalWrite(PIN_DIR_DRIVE_A, (cmd.motor_flags & FLAG_DRIVE_A) != 0);
  digitalWrite(PIN_DIR_DRIVE_B, (cmd.motor_flags & FLAG_DRIVE_B) != 0);
  digitalWrite(PIN_DIR_STEER_A, (cmd.motor_flags & FLAG_STEER_A) != 0);
  digitalWrite(PIN_DIR_STEER_B, (cmd.motor_flags & FLAG_STEER_B) != 0);

  // 2) Wypelnienie PWM (moc).
  analogWrite(PIN_PWM_DRIVE, cmd.pwm_drive);
  analogWrite(PIN_PWM_STEER, cmd.pwm_steer);
}

// ============================================================
// Task 1: sieciowy (odbiera i filtruje UDP)
// ============================================================
void udpReceiverTask(void *pvParameters)
{
  (void)pvParameters;

  // highest_seq_id przechowuje ostatnia zaakceptowana komende.
  uint16_t highest_seq_id = 0;
  bool has_seq_baseline = false;

  for (;;)
  {
    int packetSize = udp.parsePacket();
    if (packetSize)
    {
      if (packetSize == sizeof(RC_Command))
      {
        RC_Command incomingCmd{};
        int bytesRead = udp.read(reinterpret_cast<uint8_t *>(&incomingCmd), sizeof(RC_Command));

        if (bytesRead == sizeof(RC_Command))
        {
          // Przepuszczamy pierwsza ramke albo kazda "nowsza" sekwencyjnie.
          bool isFirstPacket = !has_seq_baseline;
          bool isNewPacket = isSeqNewer(incomingCmd.seq_id, highest_seq_id);

          if (isFirstPacket || isNewPacket)
          {
            highest_seq_id = incomingCmd.seq_id;
            has_seq_baseline = true;

            DBG_PRINTF("[UDP] ID: %u | P_Drv: %u | P_Str: %u | Flags: %c%c%c%c\n",
                       incomingCmd.seq_id,
                       incomingCmd.pwm_drive,
                       incomingCmd.pwm_steer,
                       (incomingCmd.motor_flags & 0b00001000) ? '1' : '0',
                       (incomingCmd.motor_flags & 0b00000100) ? '1' : '0',
                       (incomingCmd.motor_flags & 0b00000010) ? '1' : '0',
                       (incomingCmd.motor_flags & 0b00000001) ? '1' : '0');

            // xQueueOverwrite + kolejka o dlugosci 1 = zawsze wygrywa najnowsza komenda.
            xQueueOverwrite(rcCommandQueue, &incomingCmd);
          }
          else
          {
            DBG_PRINTF("[UDP DROP] Stary/duplikat seq_id: %u\n", incomingCmd.seq_id);
          }
        }
      }
      else
      {
        // Odrzucamy niezgodny pakiet (inna dlugosc niz spodziewane 7 bajtow).
        while (udp.available() > 0)
        {
          (void)udp.read();
        }
        DBG_PRINTF("[UDP DROP] Nieprawidlowy rozmiar pakietu: %d\n", packetSize);
      }
    }

    // Krotka pauza, aby task nie monopolizowal CPU.
    vTaskDelay(pdMS_TO_TICKS(1));
  }
}

// ============================================================
// Task 2: kontrola silnikow + failsafe
// ============================================================
void motorControlTask(void *pvParameters)
{
  (void)pvParameters;

  RC_Command currentCmd{};

  // Flaga, aby nie logowac i nie wykonywac stale tych samych operacji co 200 ms.
  bool failsafeActive = true;

  // Startujemy zawsze od stanu bezpiecznego.
  stopMotorsSafe();

  for (;;)
  {
    // Czekamy max 200 ms na nowa komende.
    // Brak komendy w tym czasie oznacza potencjalna utrate lacza.
    if (xQueueReceive(rcCommandQueue, &currentCmd, pdMS_TO_TICKS(200)) == pdPASS)
    {
      // Jesli wracamy z failsafe do sterowania, logujemy to jednokrotnie.
      if (failsafeActive)
      {
        DBG_PRINTLN("[FAILSAFE OFF] Sygnal odzyskany, przywracam sterowanie.");
        failsafeActive = false;
      }

      applyMotorCommand(currentCmd);
    }
    else
    {
      // Failsafe aktywujemy tylko raz na epizod utraty sygnalu,
      // dzieki czemu nie zalewamy UART i nie dublujemy zapisow GPIO.
      if (!failsafeActive)
      {
        DBG_PRINTLN("[FAILSAFE ON] Brak sygnalu przez 200 ms. Odcinam silniki.");
        stopMotorsSafe();
        failsafeActive = true;
      }
    }
  }
}

// ============================================================
// setup()
// ============================================================
void setup()
{
  DBG_BEGIN(115200);
  delay(100);

  // Konfiguracja pinow kierunku jako wyjscia.
  pinMode(PIN_DIR_DRIVE_A, OUTPUT);
  pinMode(PIN_DIR_DRIVE_B, OUTPUT);
  pinMode(PIN_DIR_STEER_A, OUTPUT);
  pinMode(PIN_DIR_STEER_B, OUTPUT);

  // Zanim uruchomimy lacznosc i taski: stan bezpieczny napedu.
  stopMotorsSafe();

  // Kolejka musi istniec, zanim task UDP zacznie wpisywac komendy.
  rcCommandQueue = xQueueCreate(1, sizeof(RC_Command));
  if (rcCommandQueue == nullptr)
  {
    DBG_PRINTLN("[FATAL] Nie udalo sie utworzyc kolejki RC. Zatrzymuje system.");
    while (true)
    {
      stopMotorsSafe();
      delay(500);
    }
  }

  // Dla niskich opoznien sterowania ustawiamy tryb station i usypianie OFF.
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);

  // Laczenie z WiFi.
  DBG_PRINT("Laczenie z WiFi");
  WiFi.begin(ssid, password);
  while (WiFi.status() != WL_CONNECTED)
  {
    delay(500);
    DBG_PRINT(".");
  }
  DBG_PRINTLN("\nWiFi polaczone! IP: " + WiFi.localIP().toString());

  // Start gniazda UDP (nasluch komend).
  if (!udp.begin(localUdpPort))
  {
    DBG_PRINTLN("[FATAL] Nie udalo sie uruchomic UDP. Zatrzymuje system.");
    while (true)
    {
      stopMotorsSafe();
      delay(500);
    }
  }
  DBG_PRINTF("Nasluchiwanie UDP na porcie %d...\n", localUdpPort);

  // Uruchomienie tasku sieciowego na rdzeniu 0 (zwykle rdzen "komunikacyjny").
  BaseType_t udpTaskOk = xTaskCreatePinnedToCore(
      udpReceiverTask,
      "UDP_Task",
      4096,
      nullptr,
      2,
      nullptr,
      0);

  // Uruchomienie tasku silnikowego na rdzeniu 1.
  BaseType_t motorTaskOk = xTaskCreatePinnedToCore(
      motorControlTask,
      "Motor_Task",
      4096,
      nullptr,
      3,
      nullptr,
      1);

  if (udpTaskOk != pdPASS || motorTaskOk != pdPASS)
  {
    DBG_PRINTLN("[FATAL] Nie udalo sie uruchomic taskow. Zatrzymuje system.");
    while (true)
    {
      stopMotorsSafe();
      delay(500);
    }
  }
}

void loop()
{
  // Przy architekturze RTOS cale sterowanie dzieje sie w taskach.
  // Usuwamy loop(), aby nie zuzywac niepotrzebnie czasu CPU.
  vTaskDelete(NULL);
}
