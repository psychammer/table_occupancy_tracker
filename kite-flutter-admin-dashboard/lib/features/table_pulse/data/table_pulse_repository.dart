import 'dart:async';
import 'dart:convert';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:http/http.dart' as http;

typedef JsonMap = Map<String, dynamic>;
typedef JsonMapList = List<JsonMap>;
typedef DoubleRow = List<double>;
typedef HeatmapGrid = List<DoubleRow>;
typedef StringList = List<String>;

const String kTablePulseApiUrl = String.fromEnvironment(
  'KITE_API_URL',
  defaultValue: 'http://127.0.0.1:8020',
);

class TableStat {
  final String name;
  final String state; // "free" | "occupied" | "needs_clearing"
  final bool occupied;
  final int guests;
  final int durationSec;
  final int dirtySec;
  final int vacantSec;
  final bool checkDue;
  final bool clearingOverdue;
  final bool vacantOverdue;
  final List<int> takenSeats;
  final int totalSeats;

  TableStat({
    required this.name,
    required this.state,
    required this.occupied,
    required this.guests,
    required this.durationSec,
    required this.dirtySec,
    required this.vacantSec,
    required this.checkDue,
    required this.clearingOverdue,
    required this.vacantOverdue,
    required this.takenSeats,
    required this.totalSeats,
  });

  bool get needsClearing => state == 'needs_clearing';

  factory TableStat.fromJson(String name, JsonMap json) {
    return TableStat(
      name: name,
      state: json['state'] as String? ?? 'free',
      occupied: json['occupied'] as bool? ?? false,
      guests: json['guests'] as int? ?? 0,
      durationSec: json['duration_sec'] as int? ?? 0,
      dirtySec: json['dirty_sec'] as int? ?? 0,
      vacantSec: json['vacant_sec'] as int? ?? 0,
      checkDue: json['check_due'] as bool? ?? false,
      clearingOverdue: json['clearing_overdue'] as bool? ?? false,
      vacantOverdue: json['vacant_overdue'] as bool? ?? false,
      takenSeats: List<int>.from(json['taken_seats'] ?? []),
      totalSeats: json['total_seats'] as int? ?? 4,
    );
  }
}

class RestaurantLiveStats {
  final int frame;
  final int occupiedTables;
  final int needsClearingTables;
  final int totalTables;
  final int totalGuests;
  final int totalSeats;
  final double utilization;
  final int avgSeatedSec;
  final Map<String, TableStat> tables;

  RestaurantLiveStats({
    required this.frame,
    required this.occupiedTables,
    required this.needsClearingTables,
    required this.totalTables,
    required this.totalGuests,
    required this.totalSeats,
    required this.utilization,
    required this.avgSeatedSec,
    required this.tables,
  });

  factory RestaurantLiveStats.fromJson(JsonMap json) {
    final rawTables = json['tables'] as JsonMap? ?? {};
    final parsedTables = <String, TableStat>{};
    rawTables.forEach((key, value) {
      parsedTables[key] = TableStat.fromJson(key, value as JsonMap);
    });

    return RestaurantLiveStats(
      frame: json['frame'] as int? ?? 0,
      occupiedTables: json['occupied_tables'] as int? ?? 0,
      needsClearingTables: json['needs_clearing_tables'] as int? ?? 0,
      totalTables: json['total_tables'] as int? ?? 0,
      totalGuests: json['total_guests'] as int? ?? 0,
      totalSeats: json['total_seats'] as int? ?? 0,
      utilization: (json['utilization'] as num? ?? 0.0).toDouble(),
      avgSeatedSec: json['avg_seated_sec'] as int? ?? 0,
      tables: parsedTables,
    );
  }
}

class StaffEvent {
  final int id;
  final String type;
  final String table;
  final double ts;
  final String time;
  final int? guests;
  final int? seatedSec;
  final int? dirtySec;
  final int? vacantSec;
  final double? durationSec;
  final double? clearDurationSec;

  StaffEvent({
    required this.id,
    required this.type,
    required this.table,
    required this.ts,
    required this.time,
    this.guests,
    this.seatedSec,
    this.dirtySec,
    this.vacantSec,
    this.durationSec,
    this.clearDurationSec,
  });

  factory StaffEvent.fromJson(JsonMap json) {
    return StaffEvent(
      id: json['id'] as int? ?? 0,
      type: json['type'] as String? ?? '',
      table: json['table'] as String? ?? '',
      ts: (json['ts'] as num? ?? 0).toDouble(),
      time: json['time'] as String? ?? '',
      guests: json['guests'] as int?,
      seatedSec: json['seated_sec'] as int?,
      dirtySec: json['dirty_sec'] as int?,
      vacantSec: json['vacant_sec'] as int?,
      durationSec: (json['duration_sec'] as num?)?.toDouble(),
      clearDurationSec: (json['clear_duration_sec'] as num?)?.toDouble(),
    );
  }
}

class AnalyticsData {
  final int days;
  final int tablesPerServer;
  final JsonMap summary;
  final JsonMapList turnoverRanking;
  final StringList seatingOpportunities;
  final JsonMap turnoverSimulator;
  final HeatmapGrid heatmap;
  final DoubleRow byHour;
  final JsonMapList busiest;

  AnalyticsData({
    required this.days,
    required this.tablesPerServer,
    required this.summary,
    required this.turnoverRanking,
    required this.seatingOpportunities,
    required this.turnoverSimulator,
    required this.heatmap,
    required this.byHour,
    required this.busiest,
  });

  factory AnalyticsData.fromJson(JsonMap json) {
    final rawHeat = json['heatmap'] as List<dynamic>? ?? [];
    final heat = rawHeat
        .map((row) => (row as List<dynamic>).map((v) => (v as num).toDouble()).toList())
        .toList();
    final rawHours = json['by_hour'] as List<dynamic>? ?? [];
    final byHour = rawHours.map((v) => (v as num).toDouble()).toList();
    final rawBusiest = json['busiest'] as List<dynamic>? ?? [];
    final busiestList = rawBusiest.map((b) => JsonMap.from(b as Map)).toList();
    final rawRanking = json['turnover_ranking'] as List<dynamic>? ?? [];
    final rankingList = rawRanking.map((r) => JsonMap.from(r as Map)).toList();
    final rawOpps = json['seating_opportunities'] as List<dynamic>? ?? [];
    final oppsList = rawOpps.map((o) => o.toString()).toList();

    return AnalyticsData(
      days: json['days'] as int? ?? 30,
      tablesPerServer: json['tables_per_server'] as int? ?? 4,
      summary: JsonMap.from(json['summary'] as Map? ?? {}),
      turnoverRanking: rankingList,
      seatingOpportunities: oppsList,
      turnoverSimulator: JsonMap.from(json['turnover_simulator'] as Map? ?? {}),
      heatmap: heat,
      byHour: byHour,
      busiest: busiestList,
    );
  }
}

class TablePulseRepository {
  final http.Client _client;
  final String baseUrl;

  TablePulseRepository(this._client, {this.baseUrl = kTablePulseApiUrl});

  Future<RestaurantLiveStats> fetchLiveStats() async {
    final res = await _client.get(Uri.parse('$baseUrl/api/stats'));
    if (res.statusCode != 200) throw Exception('Failed to load stats');
    return RestaurantLiveStats.fromJson(jsonDecode(res.body) as JsonMap);
  }

  Future<void> markTableCleared(String tableName) async {
    final encoded = Uri.encodeComponent(tableName);
    final res = await _client.post(Uri.parse('$baseUrl/api/tables/$encoded/cleared'));
    if (res.statusCode != 200) throw Exception('Failed to mark $tableName cleared');
  }

  Future<List<StaffEvent>> fetchEvents({int since = 0}) async {
    final res = await _client.get(Uri.parse('$baseUrl/api/events?since=$since'));
    if (res.statusCode != 200) return [];
    final data = jsonDecode(res.body) as JsonMap;
    return (data['events'] as List<dynamic>? ?? [])
        .map((e) => StaffEvent.fromJson(e as JsonMap))
        .toList();
  }

  Future<AnalyticsData> fetchAnalytics({int days = 30, int tablesPerServer = 4}) async {
    final res = await _client.get(
      Uri.parse('$baseUrl/api/analytics?days=$days&tables_per_server=$tablesPerServer'),
    );
    if (res.statusCode != 200) throw Exception('Failed to load analytics');
    return AnalyticsData.fromJson(jsonDecode(res.body) as JsonMap);
  }

  Future<JsonMapList> fetchVisitHistory() async {
    final res = await _client.get(Uri.parse('$baseUrl/api/history'));
    if (res.statusCode != 200) throw Exception('Failed to load history');
    final data = jsonDecode(res.body) as JsonMap;
    final rawList = data['history'] as List<dynamic>? ?? [];
    return rawList.map((item) => JsonMap.from(item as Map)).toList();
  }

  Future<JsonMap> fetchSettings() async {
    final res = await _client.get(Uri.parse('$baseUrl/api/settings'));
    if (res.statusCode != 200) throw Exception('Failed to load settings');
    return jsonDecode(res.body) as JsonMap;
  }

  Future<void> updateSettings(JsonMap settings) async {
    final res = await _client.post(
      Uri.parse('$baseUrl/api/settings'),
      headers: {'Content-Type': 'application/json'},
      body: jsonEncode(settings),
    );
    if (res.statusCode != 200) throw Exception('Failed to save settings: ${res.body}');
  }

  Future<void> restartStream() async {
    await _client.post(Uri.parse('$baseUrl/api/restart'));
  }
}

final tablePulseRepoProvider = Provider<TablePulseRepository>((ref) {
  final client = http.Client();
  ref.onDispose(client.close);
  return TablePulseRepository(client);
});

final liveStatsStreamProvider = StreamProvider.autoDispose<RestaurantLiveStats>((ref) async* {
  final repo = ref.watch(tablePulseRepoProvider);
  while (true) {
    try {
      yield await repo.fetchLiveStats();
    } catch (_) {}
    await Future.delayed(const Duration(seconds: 1));
  }
});