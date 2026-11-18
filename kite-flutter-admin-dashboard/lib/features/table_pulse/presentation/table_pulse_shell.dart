import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import '../data/table_pulse_repository.dart';

class TablePulseShell extends ConsumerStatefulWidget {
  const TablePulseShell({super.key});

  @override
  ConsumerState<TablePulseShell> createState() => _TablePulseShellState();
}

class _TablePulseShellState extends ConsumerState<TablePulseShell> {
  int _selectedIndex = 0;
  final List<StaffEvent> _alerts = [];
  int _lastEventId = 0;
  Timer? _eventsTimer;

  @override
  void initState() {
    super.initState();
    _pollEvents();
    _eventsTimer = Timer.periodic(const Duration(seconds: 2), (_) => _pollEvents());
  }

  Future<void> _pollEvents() async {
    final repo = ref.read(tablePulseRepoProvider);
    final newEvents = await repo.fetchEvents(since: _lastEventId);
    if (!mounted || newEvents.isEmpty) return;

    final nowSec = DateTime.now().millisecondsSinceEpoch / 1000.0;
    bool shouldAlertStaff = false;

    setState(() {
      for (final e in newEvents) {
        _lastEventId = e.id > _lastEventId ? e.id : _lastEventId;
        if (e.type == 'GUESTS_LEFT') continue;
        _alerts.insert(0, e);
        if (nowSec - e.ts < 30 && (e.type == 'CHECK_ON_TABLE' || e.type == 'NEEDS_CLEARING')) {
          shouldAlertStaff = true;
        }
      }
      if (_alerts.length > 50) _alerts.removeRange(50, _alerts.length);
    });

    if (shouldAlertStaff) {
      HapticFeedback.heavyImpact();
      SystemSound.play(SystemSoundType.alert);
      final latest = _alerts.first;
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            behavior: SnackBarBehavior.floating,
            backgroundColor: const Color(0xFF18181B),
            shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(8),
              side: BorderSide(
                color: latest.type == 'NEEDS_CLEARING' ? Colors.amber : Colors.redAccent,
              ),
            ),
            content: Text(
              _formatEventText(latest),
              style: const TextStyle(color: Colors.white, fontWeight: FontWeight.w600),
            ),
            duration: const Duration(seconds: 4),
          ),
        );
      }
    }
  }

  String _formatEventText(StaffEvent e) {
    switch (e.type) {
      case 'SEATED':
        return '${e.table}: guests seated (${e.guests ?? 1})';
      case 'CHECK_ON_TABLE':
        final mins = ((e.seatedSec ?? 0) / 60).round();
        return '⚠️ Check on ${e.table} ($mins min seated)';
      case 'NEEDS_CLEARING':
        return '🧹 ${e.table} needs clearing';
      case 'CLEARED':
        return '✅ ${e.table} cleared';
      default:
        return '${e.table}: ${e.type}';
    }
  }

  @override
  void dispose() {
    _eventsTimer?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final pages = [
      _LiveFloorTab(alerts: _alerts, formatEventText: _formatEventText),
      const _AnalyticsAndHistoryTab(),
      const _SettingsTab(),
    ];

    final hasUrgentAlert = _alerts.any(
      (a) => a.type == 'NEEDS_CLEARING' || a.type == 'CHECK_ON_TABLE',
    );

    return Scaffold(
      body: SafeArea(child: pages[_selectedIndex]),
      bottomNavigationBar: Container(
        decoration: const BoxDecoration(
          border: Border(top: BorderSide(color: Color(0xFF27272A), width: 1)),
        ),
        child: NavigationBar(
          height: 64,
          selectedIndex: _selectedIndex,
          onDestinationSelected: (idx) => setState(() => _selectedIndex = idx),
          destinations: [
            NavigationDestination(
              icon: Badge(
                isLabelVisible: hasUrgentAlert,
                child: const Icon(Icons.grid_view_rounded),
              ),
              label: 'Live Floor',
            ),
            const NavigationDestination(
              icon: Icon(Icons.bar_chart_rounded),
              label: 'Analytics',
            ),
            const NavigationDestination(
              icon: Icon(Icons.tune_rounded),
              label: 'Settings',
            ),
          ],
        ),
      ),
    );
  }
}

// ============================================================================
// TAB 1: LIVE FLOOR, NEEDS-CLEARING ACTIONS & STAFF ALERTS
// ============================================================================
class _LiveFloorTab extends ConsumerStatefulWidget {
  final List<StaffEvent> alerts;
  final String Function(StaffEvent) formatEventText;

  const _LiveFloorTab({required this.alerts, required this.formatEventText});

  @override
  ConsumerState<_LiveFloorTab> createState() => _LiveFloorTabState();
}

class _LiveFloorTabState extends ConsumerState<_LiveFloorTab> {
  int _frameTick = 0;
  Timer? _camTimer;
  bool _showCamera = true;

  @override
  void initState() {
    super.initState();
    _camTimer = Timer.periodic(const Duration(milliseconds: 250), (_) {
      if (mounted && _showCamera) setState(() => _frameTick++);
    });
  }

  @override
  void dispose() {
    _camTimer?.cancel();
    super.dispose();
  }

  String _fmtDur(int sec) {
    final m = sec ~/ 60;
    final s = (sec % 60).toString().padLeft(2, '0');
    return '$m:$s';
  }

  @override
  Widget build(BuildContext context) {
    final statsAsync = ref.watch(liveStatsStreamProvider);

    return Column(
      children: [
        // Kite Top Header Bar
        Container(
          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
          decoration: const BoxDecoration(
            border: Border(bottom: BorderSide(color: Color(0xFF27272A))),
          ),
          child: Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              const Row(
                children: [
                  Icon(Icons.radar, color: Color(0xFF38BDF8), size: 20),
                  SizedBox(width: 8),
                  Text(
                    'TablePulse',
                    style: TextStyle(fontSize: 17, fontWeight: FontWeight.w700, letterSpacing: -0.3),
                  ),
                ],
              ),
              Row(
                children: [
                  OutlinedButton.icon(
                    onPressed: () => setState(() => _showCamera = !_showCamera),
                    icon: Icon(_showCamera ? Icons.videocam_outlined : Icons.videocam_off_outlined, size: 16),
                    label: Text(_showCamera ? 'Hide Cam' : 'Show Cam', style: const TextStyle(fontSize: 12)),
                  ),
                  const SizedBox(width: 8),
                  IconButton(
                    tooltip: 'Restart Video',
                    onPressed: () => ref.read(tablePulseRepoProvider).restartStream(),
                    icon: const Icon(Icons.refresh, size: 18),
                  ),
                ],
              ),
            ],
          ),
        ),
        Expanded(
          child: statsAsync.when(
            loading: () => const Center(child: CircularProgressIndicator()),
            error: (e, _) => Center(
              child: Padding(
                padding: const EdgeInsets.all(24),
                child: Text(
                  'Connecting to TablePulse server at:\n$kTablePulseApiUrl\n\nMake sure python app.py is running.',
                  textAlign: TextAlign.center,
                  style: const TextStyle(color: Color(0xFFA1A1AA)),
                ),
              ),
            ),
            data: (stats) => ListView(
              padding: const EdgeInsets.all(16),
              children: [
                // 4 Kite Shadcn KPI Cards
                GridView.count(
                  crossAxisCount: MediaQuery.of(context).size.width > 700 ? 4 : 2,
                  crossAxisSpacing: 12,
                  mainAxisSpacing: 12,
                  childAspectRatio: 1.75,
                  shrinkWrap: true,
                  physics: const NeverScrollableScrollPhysics(),
                  children: [
                    _KiteKpiCard(
                      title: 'Occupied Tables',
                      value: '${stats.occupiedTables} / ${stats.totalTables}',
                      badge: stats.needsClearingTables > 0
                          ? '${stats.needsClearingTables} Dirty'
                          : 'Live',
                      badgeColor: stats.needsClearingTables > 0 ? Colors.amber : Colors.greenAccent,
                    ),
                    _KiteKpiCard(
                      title: 'Guests Seated',
                      value: '${stats.totalGuests}',
                      badge: '${stats.totalSeats} Cap',
                      badgeColor: const Color(0xFF38BDF8),
                    ),
                    _KiteKpiCard(
                      title: 'Utilization',
                      value: '${stats.utilization}%',
                      badge: 'Floor',
                      badgeColor: Colors.purpleAccent,
                    ),
                    _KiteKpiCard(
                      title: 'Avg Dwell Time',
                      value: _fmtDur(stats.avgSeatedSec),
                      badge: 'Active',
                      badgeColor: Colors.orangeAccent,
                    ),
                  ],
                ),
                const SizedBox(height: 16),

                // Collapsible CCTV Feed
                if (_showCamera) ...[
                  Card(
                    clipBehavior: Clip.antiAlias,
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        const Padding(
                          padding: EdgeInsets.symmetric(horizontal: 14, vertical: 10),
                          child: Row(
                            children: [
                              Icon(Icons.circle, color: Colors.redAccent, size: 10),
                              SizedBox(width: 8),
                              Text('Live CCTV Feed', style: TextStyle(fontWeight: FontWeight.w600, fontSize: 13)),
                            ],
                          ),
                        ),
                        AspectRatio(
                          aspectRatio: 16 / 9,
                          child: Image.network(
                            '$kTablePulseApiUrl/api/snapshot.jpg?t=$_frameTick',
                            fit: BoxFit.cover,
                            gaplessPlayback: true,
                            errorBuilder: (_, __, ___) => const Center(
                              child: Text('Waiting for video frames...', style: TextStyle(color: Color(0xFFA1A1AA))),
                            ),
                          ),
                        ),
                      ],
                    ),
                  ),
                  const SizedBox(height: 16),
                ],

                // Interactive Floor Plan
                const Text(
                  'Interactive Floor Plan',
                  style: TextStyle(fontSize: 15, fontWeight: FontWeight.w600),
                ),
                const SizedBox(height: 10),
                GridView.builder(
                  shrinkWrap: true,
                  physics: const NeverScrollableScrollPhysics(),
                  gridDelegate: const SliverGridDelegateWithMaxCrossAxisExtent(
                    maxCrossAxisExtent: 230,
                    mainAxisSpacing: 12,
                    crossAxisSpacing: 12,
                    childAspectRatio: 1.05,
                  ),
                  itemCount: stats.tables.length,
                  itemBuilder: (context, idx) {
                    final t = stats.tables.values.elementAt(idx);
                    final Color statusColor = t.occupied
                        ? const Color(0xFFEF4444)
                        : t.needsClearing
                            ? const Color(0xFFF59E0B)
                            : const Color(0xFF22C55E);

                    return Container(
                      padding: const EdgeInsets.all(12),
                      decoration: BoxDecoration(
                        color: (t.occupied || t.needsClearing)
                            ? statusColor.withOpacity(0.10)
                            : const Color(0xFF121215),
                        borderRadius: BorderRadius.circular(10),
                        border: Border.all(color: statusColor.withOpacity(0.8), width: 1.5),
                      ),
                      child: Column(
                        mainAxisAlignment: MainAxisAlignment.spaceBetween,
                        children: [
                          Row(
                            mainAxisAlignment: MainAxisAlignment.spaceBetween,
                            children: [
                              Text(t.name, style: const TextStyle(fontWeight: FontWeight.w700, fontSize: 15)),
                              _StatusPill(
                                label: t.occupied
                                    ? 'BUSY'
                                    : t.needsClearing
                                        ? 'CLEAR'
                                        : 'FREE',
                                color: statusColor,
                              ),
                            ],
                          ),
                          // Seat Dots
                          Row(
                            mainAxisAlignment: MainAxisAlignment.center,
                            children: List.generate(t.totalSeats, (i) {
                              final taken = t.takenSeats.contains(i + 1);
                              return Container(
                                margin: const EdgeInsets.symmetric(horizontal: 4),
                                width: 14,
                                height: 14,
                                decoration: BoxDecoration(
                                  shape: BoxShape.circle,
                                  color: taken ? const Color(0xFFEF4444) : const Color(0xFF27272A),
                                  border: Border.all(
                                    color: taken ? Colors.redAccent : const Color(0xFF3F3F46),
                                  ),
                                ),
                              );
                            }),
                          ),
                          if (t.occupied)
                            Text(
                              '👥 ${t.guests} Guests • ${_fmtDur(t.durationSec)}${t.checkDue ? ' ⚠️' : ''}',
                              style: const TextStyle(fontSize: 12, color: Color(0xFFE4E4E7)),
                              textAlign: TextAlign.center,
                            )
                          else if (t.needsClearing)
                            SizedBox(
                              width: double.infinity,
                              height: 30,
                              child: FilledButton(
                                style: FilledButton.styleFrom(
                                  backgroundColor: const Color(0xFFF59E0B),
                                  foregroundColor: Colors.black,
                                  padding: const EdgeInsets.symmetric(horizontal: 8),
                                ),
                                onPressed: () async {
                                  HapticFeedback.mediumImpact();
                                  await ref.read(tablePulseRepoProvider).markTableCleared(t.name);
                                  ref.invalidate(liveStatsStreamProvider);
                                },
                                child: Text(
                                  'Mark Cleared (${_fmtDur(t.dirtySec)})',
                                  style: const TextStyle(fontSize: 11, fontWeight: FontWeight.bold),
                                ),
                              ),
                            )
                          else
                            const Text('Available', style: TextStyle(fontSize: 12, color: Color(0xFFA1A1AA))),
                        ],
                      ),
                    );
                  },
                ),
                const SizedBox(height: 20),

                // Staff Alerts Card
                const Text(
                  'Real-Time Staff Alerts',
                  style: TextStyle(fontSize: 15, fontWeight: FontWeight.w600),
                ),
                const SizedBox(height: 10),
                Card(
                  child: widget.alerts.isEmpty
                      ? const Padding(
                          padding: EdgeInsets.all(16),
                          child: Text(
                            'No alerts yet. Seating, check-in, and table clearing alerts appear here.',
                            style: TextStyle(color: Color(0xFFA1A1AA), fontSize: 13),
                          ),
                        )
                      : ListView.separated(
                          shrinkWrap: true,
                          physics: const NeverScrollableScrollPhysics(),
                          itemCount: widget.alerts.take(15).length,
                          separatorBuilder: (_, __) => const Divider(height: 1),
                          itemBuilder: (context, i) {
                            final a = widget.alerts[i];
                            final Color dot = a.type == 'NEEDS_CLEARING'
                                ? Colors.redAccent
                                : a.type == 'CHECK_ON_TABLE'
                                    ? Colors.amber
                                    : Colors.greenAccent;
                            return ListTile(
                              dense: true,
                              leading: Icon(Icons.circle, size: 10, color: dot),
                              title: Text(widget.formatEventText(a), style: const TextStyle(fontSize: 13)),
                              trailing: Text(
                                a.time,
                                style: const TextStyle(fontSize: 11, color: Color(0xFFA1A1AA)),
                              ),
                            );
                          },
                        ),
                ),
              ],
            ),
          ),
        ),
      ],
    );
  }
}

// ============================================================================
// TAB 2: BUSY HOURS ANALYTICS & COMPLETED VISITS
// ============================================================================
class _AnalyticsBundle {
  final AnalyticsData analytics;
  final JsonMapList history;
  _AnalyticsBundle(this.analytics, this.history);
}

class _AnalyticsAndHistoryTab extends ConsumerWidget {
  const _AnalyticsAndHistoryTab();
  static const _days = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

  Future<_AnalyticsBundle> _loadBundle(TablePulseRepository repo) async {
    final an = await repo.fetchAnalytics();
    final hist = await repo.fetchVisitHistory();
    return _AnalyticsBundle(an, hist);
  }

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final repo = ref.watch(tablePulseRepoProvider);

    return Scaffold(
      appBar: AppBar(title: const Text('History & Busy Hours (30d)', style: TextStyle(fontSize: 17, fontWeight: FontWeight.w700))),
      body: FutureBuilder<_AnalyticsBundle>(
        future: _loadBundle(repo),
        builder: (context, snap) {
          if (snap.hasError) {
            return Center(child: Text('Error: ${snap.error}'));
          }
          if (!snap.hasData) return const Center(child: CircularProgressIndicator());
          final an = snap.data!.analytics;
          final history = snap.data!.history;
          final s = an.summary;
          final maxHourVal = an.byHour.fold<double>(0.01, (p, c) => c > p ? c : p);

          return ListView(
            padding: const EdgeInsets.all(16),
            children: [
              GridView.count(
                crossAxisCount: 2,
                crossAxisSpacing: 12,
                mainAxisSpacing: 12,
                childAspectRatio: 1.85,
                shrinkWrap: true,
                physics: const NeverScrollableScrollPhysics(),
                children: [
                  _KiteKpiCard(title: 'Total Visits', value: '${s['visits'] ?? 0}', badge: '30d', badgeColor: Colors.lightBlueAccent),
                  _KiteKpiCard(
                    title: 'Avg Seated Time',
                    value: '${((s['avg_duration_sec'] ?? 0) / 60).round()}m',
                    badge: 'Dwell',
                    badgeColor: Colors.amber,
                  ),
                  _KiteKpiCard(title: 'Avg Party Size', value: '${s['avg_party_size'] ?? 0}', badge: 'Guests', badgeColor: Colors.greenAccent),
                  _KiteKpiCard(
                    title: 'Peak Tables',
                    value: '${s['peak_occupied'] ?? 0}/${s['total_tables'] ?? 0}',
                    badge: 'Peak',
                    badgeColor: Colors.redAccent,
                  ),
                ],
              ),
              const SizedBox(height: 16),
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(14),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      const Text('Staffing Recommendation', style: TextStyle(fontWeight: FontWeight.w600)),
                      const SizedBox(height: 6),
                      Text(
                        an.busiest.isNotEmpty
                            ? an.busiest
                                .map((b) =>
                                    '• ${_days[b['weekday'] as int]} ${(b['hour'] as int).toString().padLeft(2, '0')}:00 → ${b['servers_needed']} server(s) (avg ${b['avg_occupied']} tables)')
                                .join('\n')
                            : 'Occupancy is sampled every 60s. Keep the camera running to populate staffing hints.',
                        style: const TextStyle(color: Color(0xFFA1A1AA), fontSize: 13),
                      ),
                    ],
                  ),
                ),
              ),
              const SizedBox(height: 16),
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(14),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      const Text('Average Occupied Tables by Hour', style: TextStyle(fontWeight: FontWeight.w600)),
                      const SizedBox(height: 12),
                      SizedBox(
                        height: 110,
                        child: Row(
                          crossAxisAlignment: CrossAxisAlignment.end,
                          children: List.generate(24, (h) {
                            final val = an.byHour.length > h ? an.byHour[h] : 0.0;
                            final factor = (val / maxHourVal).clamp(0.03, 1.0);
                            return Expanded(
                              child: Padding(
                                padding: const EdgeInsets.symmetric(horizontal: 1.5),
                                child: Column(
                                  mainAxisAlignment: MainAxisAlignment.end,
                                  children: [
                                    Expanded(
                                      child: Align(
                                        alignment: Alignment.bottomCenter,
                                        child: FractionallySizedBox(
                                          heightFactor: factor,
                                          child: Container(
                                            decoration: BoxDecoration(
                                              color: const Color(0xFF38BDF8),
                                              borderRadius: BorderRadius.circular(2),
                                            ),
                                          ),
                                        ),
                                      ),
                                    ),
                                    const SizedBox(height: 4),
                                    Text(h % 3 == 0 ? '$h' : '', style: const TextStyle(fontSize: 9, color: Color(0xFFA1A1AA))),
                                  ],
                                ),
                              ),
                            );
                          }),
                        ),
                      ),
                    ],
                  ),
                ),
              ),
              const SizedBox(height: 16),
              const Text('Completed Table Visits', style: TextStyle(fontSize: 15, fontWeight: FontWeight.w600)),
              const SizedBox(height: 8),
              Card(
                child: history.isEmpty
                    ? const Padding(
                        padding: EdgeInsets.all(16),
                        child: Text('No completed visits logged yet.', style: TextStyle(color: Color(0xFFA1A1AA))),
                      )
                    : ListView.separated(
                        shrinkWrap: true,
                        physics: const NeverScrollableScrollPhysics(),
                        itemCount: history.length,
                        separatorBuilder: (_, __) => const Divider(height: 1),
                        itemBuilder: (context, i) {
                          final r = history[i];
                          return ListTile(
                            dense: true,
                            title: Text('#${r['id']} • ${r['table_name']} (${r['guests']} guests)'),
                            subtitle: Text('${r['timestamp']}', style: const TextStyle(fontSize: 11, color: Color(0xFFA1A1AA))),
                            trailing: _StatusPill(label: '${r['duration_sec']}s', color: const Color(0xFF38BDF8)),
                          );
                        },
                      ),
              ),
            ],
          );
        },
      ),
    );
  }
}

// ============================================================================
// TAB 3: PIPELINE & CCTV SETTINGS
// ============================================================================
class _SettingsTab extends ConsumerStatefulWidget {
  const _SettingsTab();

  @override
  ConsumerState<_SettingsTab> createState() => _SettingsTabState();
}

class _SettingsTabState extends ConsumerState<_SettingsTab> {
  final _videoCtrl = TextEditingController();
  final _seatsCtrl = TextEditingController();
  final _occCtrl = TextEditingController();
  final _freeCtrl = TextEditingController();
  final _checkCtrl = TextEditingController();
  final _inferCtrl = TextEditingController();
  final _upCtrl = TextEditingController();
  final _downCtrl = TextEditingController();
  final _confCtrl = TextEditingController();
  bool _stabilizeCamera = true;
  bool _loaded = false;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    final s = await ref.read(tablePulseRepoProvider).fetchSettings();
    if (!mounted) return;
    setState(() {
      _videoCtrl.text = '${s['video_path'] ?? ''}';
      _seatsCtrl.text = '${s['seats_per_table'] ?? 4}';
      _occCtrl.text = '${s['occupy_seconds'] ?? 3.0}';
      _freeCtrl.text = '${s['free_seconds'] ?? 20.0}';
      _checkCtrl.text = '${s['check_after_seconds'] ?? 300.0}';
      _inferCtrl.text = '${s['infer_fps'] ?? 4.0}';
      _upCtrl.text = '${s['up_lock_seconds'] ?? 2.0}';
      _downCtrl.text = '${s['down_lock_seconds'] ?? 10.0}';
      _confCtrl.text = '${s['confidence'] ?? 0.3}';
      _stabilizeCamera = s['stabilize_camera'] as bool? ?? true;
      _loaded = true;
    });
  }

  Future<void> _save() async {
    final payload = <String, dynamic>{
      'video_path': _videoCtrl.text.trim(),
      'seats_per_table': int.tryParse(_seatsCtrl.text) ?? 4,
      'occupy_seconds': double.tryParse(_occCtrl.text) ?? 3.0,
      'free_seconds': double.tryParse(_freeCtrl.text) ?? 20.0,
      'check_after_seconds': double.tryParse(_checkCtrl.text) ?? 300.0,
      'infer_fps': double.tryParse(_inferCtrl.text) ?? 4.0,
      'up_lock_seconds': double.tryParse(_upCtrl.text) ?? 2.0,
      'down_lock_seconds': double.tryParse(_downCtrl.text) ?? 10.0,
      'confidence': double.tryParse(_confCtrl.text) ?? 0.30,
      'stabilize_camera': _stabilizeCamera,
    };
    await ref.read(tablePulseRepoProvider).updateSettings(payload);
    if (mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Settings applied & pipeline restarted!')),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    if (!_loaded) return const Center(child: CircularProgressIndicator());
    return Scaffold(
      appBar: AppBar(title: const Text('Pipeline & CCTV Settings', style: TextStyle(fontSize: 17, fontWeight: FontWeight.w700))),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          Card(
            child: Padding(
              padding: const EdgeInsets.all(16),
              child: Column(
                children: [
                  SwitchListTile(
                    contentPadding: EdgeInsets.zero,
                    title: const Text('ORB Camera Stabilizer', style: TextStyle(fontWeight: FontWeight.w600)),
                    subtitle: const Text('Warps table polygons if camera shifts slightly', style: TextStyle(fontSize: 12, color: Color(0xFFA1A1AA))),
                    value: _stabilizeCamera,
                    onChanged: (v) => setState(() => _stabilizeCamera = v),
                  ),
                  const Divider(height: 24),
                  _input('Video Path or RTSP URL', _videoCtrl, isNumber: false),
                  _input('Seats Per Table (1–20)', _seatsCtrl),
                  _input('Occupy Timer (seconds)', _occCtrl),
                  _input('Free / Empty Timer (seconds)', _freeCtrl),
                  _input('Check-on-Table Alert After (seconds)', _checkCtrl),
                  _input('Inference Rate (FPS, 0.5–30)', _inferCtrl),
                  _input('Up Lock Hysteresis (seconds)', _upCtrl),
                  _input('Down Lock Hysteresis (seconds)', _downCtrl),
                  _input('YOLO Confidence Threshold (0–1)', _confCtrl),
                  const SizedBox(height: 12),
                  SizedBox(
                    width: double.infinity,
                    child: FilledButton.icon(
                      onPressed: _save,
                      icon: const Icon(Icons.save_outlined, size: 18),
                      label: const Text('Save & Apply Settings'),
                    ),
                  ),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }

  Widget _input(String label, TextEditingController ctrl, {bool isNumber = true}) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 12),
      child: TextField(
        controller: ctrl,
        keyboardType: isNumber ? const TextInputType.numberWithOptions(decimal: true) : TextInputType.text,
        decoration: InputDecoration(
          labelText: label,
          filled: true,
          fillColor: const Color(0xFF09090B),
          border: OutlineInputBorder(borderRadius: BorderRadius.circular(8)),
        ),
      ),
    );
  }
}

// ============================================================================
// KITE SHADCN-STYLE REUSABLE UI COMPONENTS
// ============================================================================
class _KiteKpiCard extends StatelessWidget {
  final String title;
  final String value;
  final String badge;
  final Color badgeColor;

  const _KiteKpiCard({
    required this.title,
    required this.value,
    required this.badge,
    required this.badgeColor,
  });

  @override
  Widget build(BuildContext context) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(14),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          mainAxisAlignment: MainAxisAlignment.spaceBetween,
          children: [
            Row(
              mainAxisAlignment: MainAxisAlignment.spaceBetween,
              children: [
                Flexible(
                  child: Text(
                    title,
                    style: const TextStyle(fontSize: 12, color: Color(0xFFA1A1AA), fontWeight: FontWeight.w500),
                    overflow: TextOverflow.ellipsis,
                  ),
                ),
                _StatusPill(label: badge, color: badgeColor),
              ],
            ),
            Text(
              value,
              style: const TextStyle(fontSize: 22, fontWeight: FontWeight.w700, letterSpacing: -0.5),
            ),
          ],
        ),
      ),
    );
  }
}

class _StatusPill extends StatelessWidget {
  final String label;
  final Color color;

  const _StatusPill({required this.label, required this.color});

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
      decoration: BoxDecoration(
        color: color.withOpacity(0.15),
        borderRadius: BorderRadius.circular(99),
        border: Border.all(color: color.withOpacity(0.5)),
      ),
      child: Text(
        label,
        style: TextStyle(fontSize: 10, fontWeight: FontWeight.w700, color: color),
      ),
    );
  }
}