/* ESP32-C5 Remote ID sniffer for Drone Watchtower.
 * Any aircraft that broadcasts ASTM F3411: Bluetooth legacy (UUID 0xFFFA),
 * Wi-Fi beacon message packs (OUI fa:0b:bc), or DJI's beacon TLVs (6a:5c:35). */
#include <stdio.h>
#include <string.h>
#include <inttypes.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "nvs_flash.h"
#include "esp_netif.h"
#include "esp_bt.h"
#include "esp_bt_main.h"
#include "esp_gap_ble_api.h"

static const char *TAG = "c5rid";
static const uint8_t DJI_OUI[3] = {0x6a, 0x5c, 0x35};
static const uint8_t ASTM_OUI[3] = {0xfa, 0x0b, 0xbc};

typedef struct {
    char serial[24];
    int32_t lat_e7;
    int32_t lon_e7;
    int32_t op_lat_e7;
    int32_t op_lon_e7;
    int8_t rssi;
    uint8_t channel;
    uint8_t mac[6];
    char via[8];
} det_t;

/* Last-seen state per MAC so a BT4 location advertisement (no serial in the
 * same adv) can be joined with the Basic ID that arrives separately. */
typedef struct {
    uint8_t mac[6];
    char serial[24];
    int32_t lat_e7;
    int32_t lon_e7;
    TickType_t tick;
} mac_slot_t;

#define MAC_SLOTS 16
static mac_slot_t mac_cache[MAC_SLOTS];

static QueueHandle_t det_q;
static volatile int ble_seen;
static volatile int wifi_seen;

static int32_t be_i32(const uint8_t *p)
{
    return (int32_t)((uint32_t)p[0] << 24 | (uint32_t)p[1] << 16 |
                     (uint32_t)p[2] << 8 | p[3]);
}

static int32_t le_i32(const uint8_t *p)
{
    return (int32_t)((uint32_t)p[0] | ((uint32_t)p[1] << 8) |
                     ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24));
}

static bool copy_serial(const uint8_t *src, int maxn, char *dst, int dstlen)
{
    int n = 0;
    while (n < maxn && n + 1 < dstlen && src[n] && src[n] != ' ') {
        char c = (char)src[n];
        if (!((c >= '0' && c <= '9') || (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z'))) {
            return false;
        }
        n++;
    }
    if (n == 0) {
        return false;
    }
    memcpy(dst, src, n);
    dst[n] = 0;
    return true;
}

/* ASTM Wi-Fi message pack: version/type, single-message size, count, messages. */
static bool parse_astm_pack(const uint8_t *pack, int len, det_t *out)
{
    if (len < 3 + 25) {
        return false;
    }
    uint8_t size = pack[1];
    uint8_t count = pack[2];
    if (size < 25 || size > 40 || count == 0 || count > 9) {
        return false;
    }
    if (3 + (int)count * size > len) {
        return false;
    }
    bool got = false;
    out->lat_e7 = 0;
    out->lon_e7 = 0;
    out->op_lat_e7 = 0;
    out->op_lon_e7 = 0;
    for (int n = 0; n < count; n++) {
        const uint8_t *m = pack + 3 + n * size;
        uint8_t mtype = m[0] >> 4;
        if (mtype == 0 && copy_serial(m + 2, 20, out->serial, sizeof(out->serial))) {
            got = true;
        } else if (mtype == 1 && size >= 13) {
            out->lat_e7 = le_i32(m + 5);
            out->lon_e7 = le_i32(m + 9);
        } else if (mtype == 4 && size >= 10) {
            /* System message: operator/pilot position at bytes 2..9. */
            out->op_lat_e7 = le_i32(m + 2);
            out->op_lon_e7 = le_i32(m + 6);
        }
    }
    return got;
}

static bool tlv_serial(const uint8_t *body, int len, det_t *out)
{
    int pos = 0;
    bool got = false;
    out->lat_e7 = 0;
    out->lon_e7 = 0;
    while (pos + 2 <= len) {
        uint8_t t = body[pos];
        uint8_t n = body[pos + 1];
        if (pos + 2 + n > len) {
            break;
        }
        const uint8_t *v = body + pos + 2;
        if (t == 0x03 && n > 0 && n < sizeof(out->serial)) {
            bool ok = true;
            for (int i = 0; i < n; i++) {
                char c = (char)v[i];
                if (!((c >= '0' && c <= '9') || (c >= 'A' && c <= 'Z') ||
                      (c >= 'a' && c <= 'z'))) {
                    ok = false;
                    break;
                }
            }
            if (ok) {
                memcpy(out->serial, v, n);
                out->serial[n] = 0;
                got = true;
            }
        } else if (t == 0x04 && n == 4) {
            out->lat_e7 = be_i32(v);
        } else if (t == 0x05 && n == 4) {
            out->lon_e7 = be_i32(v);
        }
        pos += 2 + n;
    }
    return got;
}

static void sniff_cb(void *buf, wifi_promiscuous_pkt_type_t type)
{
    if (type != WIFI_PKT_MGMT || det_q == NULL) {
        return;
    }
    const wifi_promiscuous_pkt_t *pkt = buf;
    int len = pkt->rx_ctrl.sig_len;
    if (len < 36 || len > 800) {
        return;
    }
    const uint8_t *p = pkt->payload;
    if ((p[0] & 0xfc) != 0x80) { /* beacon */
        return;
    }
    wifi_seen++;
    for (int i = 0; i + 10 < len; i++) {
        int dji = memcmp(p + i, DJI_OUI, 3) == 0;
        int astm = memcmp(p + i, ASTM_OUI, 3) == 0;
        if (!dji && !astm) {
            continue;
        }
        det_t d = {0};
        bool ok = dji ? tlv_serial(p + i + 7, len - (i + 7), &d)
                      : parse_astm_pack(p + i + 4, len - (i + 4), &d);
        if (!ok) {
            continue;
        }
        d.rssi = pkt->rx_ctrl.rssi;
        d.channel = pkt->rx_ctrl.channel;
        memcpy(d.mac, p + 10, 6); /* addr2, the transmitter */
        memcpy(d.via, "wifi", 5);
        xQueueSend(det_q, &d, 0);
        return;
    }
}

static void printer_task(void *arg)
{
    char last_serial[24] = {0};
    TickType_t last_tick = 0;
    det_t d;
    while (1) {
        if (xQueueReceive(det_q, &d, portMAX_DELAY) != pdTRUE) {
            continue;
        }
        TickType_t now = xTaskGetTickCount();
        if (strcmp(last_serial, d.serial) == 0 &&
            (now - last_tick) < pdMS_TO_TICKS(800)) {
            continue;
        }
        memcpy(last_serial, d.serial, sizeof(last_serial));
        last_tick = now;
        printf("{\"src\":\"c5\",\"via\":\"%s\",\"serial\":\"%s\",\"lat\":%.7f,\"lon\":%.7f,"
               "\"op_lat\":%.7f,\"op_lon\":%.7f,"
               "\"rssi\":%d,\"ch\":%u,\"mac\":\"%02x:%02x:%02x:%02x:%02x:%02x\"}\n",
               d.via[0] ? d.via : "wifi",
               d.serial, d.lat_e7 / 1e7, d.lon_e7 / 1e7,
               d.op_lat_e7 / 1e7, d.op_lon_e7 / 1e7,
               d.rssi, d.channel,
               d.mac[0], d.mac[1], d.mac[2], d.mac[3], d.mac[4], d.mac[5]);
        fflush(stdout);
    }
}

static void stats_task(void *arg)
{
    int last_ble = 0;
    while (1) {
        vTaskDelay(pdMS_TO_TICKS(2000));
        int ble = ble_seen;
        printf("{\"src\":\"c5\",\"stats\":true,\"ble\":%d,\"wifi\":%d}\n", ble - last_ble, wifi_seen);
        fflush(stdout);
        last_ble = ble;
        wifi_seen = 0;
    }
}

static int mac_slot(const uint8_t *mac)
{
    int free_i = -1;
    int oldest = 0;
    TickType_t oldest_tick = mac_cache[0].tick;
    for (int i = 0; i < MAC_SLOTS; i++) {
        if (memcmp(mac_cache[i].mac, mac, 6) == 0) {
            return i;
        }
        if (free_i < 0 && mac_cache[i].serial[0] == 0 && mac_cache[i].lat_e7 == 0) {
            free_i = i;
        }
        if (mac_cache[i].tick < oldest_tick) {
            oldest_tick = mac_cache[i].tick;
            oldest = i;
        }
    }
    return free_i >= 0 ? free_i : oldest;
}

/* ASTM F3411 Bluetooth 4 legacy: service-data UUID 0xFFFA, then a counter
 * and one 25-byte OpenDroneID message. Basic ID bytes 2..21 are the serial.
 * Location adverts carry no serial, so positions are cached per MAC and
 * joined when the Basic ID for that MAC arrives (either order). */
static bool parse_odid(const uint8_t *msg, int len, int8_t rssi, const uint8_t *bda)
{
    if (len < 25 || det_q == NULL) {
        return false;
    }
    uint8_t mtype = msg[0] >> 4;
    det_t d = {0};
    memcpy(d.via, "bt4", 4);
    d.rssi = rssi;
    d.channel = 37;
    if (bda) {
        memcpy(d.mac, bda, 6);
    }
    int slot = bda ? mac_slot(bda) : -1;
    if (mtype == 0) {
        int n = 0;
        while (n < 20 && msg[2 + n] && msg[2 + n] != ' ') {
            char c = (char)msg[2 + n];
            if (!((c >= '0' && c <= '9') || (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z'))) {
                return false;
            }
            n++;
        }
        if (n == 0 || n >= (int)sizeof(d.serial)) {
            return false;
        }
        memcpy(d.serial, msg + 2, n);
        d.serial[n] = 0;
        if (slot >= 0) {
            memcpy(mac_cache[slot].serial, d.serial, sizeof(d.serial));
            mac_cache[slot].tick = xTaskGetTickCount();
            if (mac_cache[slot].lat_e7 || mac_cache[slot].lon_e7) {
                d.lat_e7 = mac_cache[slot].lat_e7;
                d.lon_e7 = mac_cache[slot].lon_e7;
            }
        }
        xQueueSend(det_q, &d, 0);
        return true;
    }
    if (mtype == 1) {
        int32_t lat = le_i32(msg + 5);
        int32_t lon = le_i32(msg + 9);
        if (slot < 0) {
            return false;
        }
        mac_cache[slot].lat_e7 = lat;
        mac_cache[slot].lon_e7 = lon;
        mac_cache[slot].tick = xTaskGetTickCount();
        if (mac_cache[slot].serial[0]) {
            memcpy(d.serial, mac_cache[slot].serial, sizeof(d.serial));
            d.lat_e7 = lat;
            d.lon_e7 = lon;
            xQueueSend(det_q, &d, 0);
            return true;
        }
        return false;
    }
    return false;
}

static int is_odid_uuid(const uint8_t *data, int dlen)
{
    return dlen >= 2 && ((data[0] == 0xFA && data[1] == 0xFF) ||
                         (data[0] == 0xFF && data[1] == 0xFA));
}

static void parse_ble_adv(const uint8_t *adv, int len, int8_t rssi, const uint8_t *bda)
{
    ble_seen++;
    int i = 0;
    while (i + 1 < len) {
        uint8_t field = adv[i];
        if (field == 0 || i + 1 + field > len) {
            break;
        }
        uint8_t typ = adv[i + 1];
        const uint8_t *data = adv + i + 2;
        int dlen = field - 1;
        /* Service data 0x16, or manufacturer data 0xFF, carrying UUID 0xFFFA.
         * Some beacons omit the counter byte, so try both alignments. */
        /* UUID, a counter, then one more header byte, then the 25-byte
         * message. data+4 skips both; data+3 folds the header byte into the
         * serial ("123" becomes "1123"), so it is only tried when +4 finds
         * nothing at all. */
        if ((typ == 0x16 || typ == 0xFF) && is_odid_uuid(data, dlen) && dlen >= 4 + 25) {
            if (!parse_odid(data + 4, dlen - 4, rssi, bda)) {
                parse_odid(data + 3, dlen - 3, rssi, bda);
            }
        }
        i += 1 + field;
    }
    if (rssi >= -40) {
        static TickType_t last_dump;
        TickType_t now = xTaskGetTickCount();
        if (now - last_dump > pdMS_TO_TICKS(2000)) {
            last_dump = now;
            printf("{\"src\":\"c5\",\"raw\":\"");
            int n = len < 48 ? len : 48;
            for (int k = 0; k < n; k++) {
                printf("%02x", adv[k]);
            }
            printf("\",\"rssi\":%d}\n", rssi);
            fflush(stdout);
        }
    }
}

static esp_ble_scan_params_t ble_scan_params = {
    .scan_type = BLE_SCAN_TYPE_ACTIVE,
    .own_addr_type = BLE_ADDR_TYPE_PUBLIC,
    .scan_filter_policy = BLE_SCAN_FILTER_ALLOW_ALL,
    .scan_interval = 0x50,
    .scan_window = 0x30,
    .scan_duplicate = BLE_SCAN_DUPLICATE_DISABLE,
};

static void gap_cb(esp_gap_ble_cb_event_t event, esp_ble_gap_cb_param_t *param)
{
    switch (event) {
    case ESP_GAP_BLE_SCAN_PARAM_SET_COMPLETE_EVT:
        esp_ble_gap_start_scanning(0);
        break;
    case ESP_GAP_BLE_SCAN_RESULT_EVT:
        if (param->scan_rst.search_evt == ESP_GAP_SEARCH_INQ_RES_EVT) {
            int n = param->scan_rst.adv_data_len + param->scan_rst.scan_rsp_len;
            parse_ble_adv(param->scan_rst.ble_adv, n, param->scan_rst.rssi, param->scan_rst.bda);
        }
        break;
    default:
        break;
    }
}

static void ble_start(void)
{
    esp_bt_controller_config_t cfg = BT_CONTROLLER_INIT_CONFIG_DEFAULT();
    if (esp_bt_controller_init(&cfg) != ESP_OK) {
        ESP_LOGE(TAG, "bt controller init failed");
        return;
    }
    if (esp_bt_controller_enable(ESP_BT_MODE_BLE) != ESP_OK) {
        ESP_LOGE(TAG, "bt controller enable failed");
        return;
    }
    if (esp_bluedroid_init() != ESP_OK || esp_bluedroid_enable() != ESP_OK) {
        ESP_LOGE(TAG, "bluedroid enable failed");
        return;
    }
    esp_ble_gap_register_callback(gap_cb);
    esp_ble_gap_set_scan_params(&ble_scan_params);
}

static void hop_task(void *arg)
{
    static const struct {
        uint8_t ch;
        uint16_t ms;
    } hop[] = {
        {6, 500}, {1, 180}, {6, 500}, {11, 180},
        {6, 500}, {36, 180}, {40, 180}, {44, 180}, {48, 180},
        {6, 500}, {149, 180}, {153, 180}, {157, 180}, {161, 180}, {165, 180},
    };
    int n = sizeof(hop) / sizeof(hop[0]);
    int i = 0;
    while (1) {
        /* BLE legacy adverts need the radio. Promiscuous Wi-Fi otherwise holds it. */
        esp_wifi_set_promiscuous(false);
        vTaskDelay(pdMS_TO_TICKS(700));
        esp_wifi_set_promiscuous(true);
        esp_err_t err = esp_wifi_set_channel(hop[i].ch, WIFI_SECOND_CHAN_NONE);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "channel %u failed: %s", hop[i].ch, esp_err_to_name(err));
        }
        vTaskDelay(pdMS_TO_TICKS(hop[i].ms));
        i = (i + 1) % n;
    }
}

void app_main(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ESP_ERROR_CHECK(nvs_flash_init());
    }
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_country_code("HK", false));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_band_mode(WIFI_BAND_MODE_AUTO));

    wifi_promiscuous_filter_t filt = {.filter_mask = WIFI_PROMIS_FILTER_MASK_MGMT};
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous_filter(&filt));
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous_rx_cb(sniff_cb));
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous(true));

    det_q = xQueueCreate(12, sizeof(det_t));
    ble_start();
    xTaskCreate(printer_task, "print", 4096, NULL, 5, NULL);
    xTaskCreate(stats_task, "stats", 3072, NULL, 3, NULL);
    xTaskCreate(hop_task, "hop", 3072, NULL, 4, NULL);
    printf("{\"src\":\"c5\",\"ready\":true}\n");
    fflush(stdout);
    ESP_LOGI(TAG, "sniffer running");
}
