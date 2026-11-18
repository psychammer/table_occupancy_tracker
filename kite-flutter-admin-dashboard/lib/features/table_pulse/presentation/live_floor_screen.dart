import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import '../data/table_pulse_repository.dart';

class LiveFloorScreen extends ConsumerStatefulWidget {
  const LiveFloorScreen({super.key});

  @override
  ConsumerState<LiveFloorScreen> createState() => _LiveFloorScreenState();
}

class _LiveFloorScreenState extends ConsumerState<LiveFloorScreen> {
  int _frameTick = 0;
  Timer? _cameraTimer;
  bool _showCameraPreview = true;

  @override
  void initState() {
    super.initState();
    // Refresh mobile snapshot image 4 times per second when camera preview is expanded
    _cameraTimer = Timer.periodic(const Duration(milliseconds: 250), (_) {
      if (mounted && _showCameraPreview) {
        setState(() => _frameTick++);
      }
    });
  }

  @override
  void dispose() {
    _cameraTimer?.cancel();
    super.dispose();
  }

  String _formatDuration(int totalSeconds) {
    final mins = (totalSeconds ~/ 60).toString().padLeft(2, '0');
    final secs = (totalSeconds % 60).toString().padLeft(2, '0');
    return '$mins:$secs';
  }

  @override
  Widget build(BuildContext context) {
    final statsAsync = ref.watch(liveStatsStreamProvider);
    final theme = Theme.of(context);

    return Scaffold(
      appBar: AppBar(
        title: const Text('TablePulse Live Floor'),
        actions: [
          IconButton(
            tooltip: 'Toggle CCTV Feed',
            icon: Icon(_showCameraPreview ? Icons.videocam : Icons.videocam_off),
            onPressed: () => setState(() => _showCameraPreview = !_showCameraPreview),
          ),
          IconButton(
            tooltip: 'Restart Video Stream',
            icon: const Icon(Icons.refresh),
            onPressed: () => ref.read(tablePulseRepoProvider).restartStream(),
          ),
        ],
      ),
      body: statsAsync.when(
        loading: () => const Center(child: CircularProgressIndicator()),
        error: (err, _) => Center(child: Text('Connecting to TablePulse Server...\n$err')),
        data: (stats) {
          return RefreshIndicator(
            onRefresh: () async => ref.invalidate(liveStatsStreamProvider),
            child: ListView(
              padding: const EdgeInsets.all(16),
              children: [
                // 1. Mobile KPI Summary Grid (2x2 on phones, 4x1 on tablets)
                LayoutBuilder(
                  builder: (context, constraints) {
                    final isWide = constraints.maxWidth > 700;
                    return GridView.count(
                      crossAxisCount: isWide ? 4 : 2,
                      crossAxisSpacing: 12,
                      mainAxisSpacing: 12,
                      childAspectRatio: isWide ? 2.2 : 1.7,
                      shrinkWrap: true,
                      physics: const NeverScrollableScrollPhysics(),
                      children: [
                        _KpiCard(
                          label: 'Occupied Tables',
                          value: '${stats.occupiedTables} / ${stats.totalTables}',
                          icon: Icons.table_restaurant,
                          color: Colors.redAccent,
                        ),
                        _KpiCard(
                          label: 'Guests Seated',
                          value: '${stats.totalGuests} / ${stats.totalSeats}',
                          icon: Icons.people_alt,
                          color: Colors.blueAccent,
                        ),
                        _KpiCard(
                          label: 'Seat Utilization',
                          value: '${stats.utilization.toStringAsFixed(0)}%',
                          icon: Icons.pie_chart,
                          color: Colors.green,
                        ),
                        _KpiCard(
                          label: 'Avg Seated Time',
                          value: _formatDuration(stats.avgSeatedSec),
                          icon: Icons.timer_outlined,
                          color: Colors.amber,
                        ),
                      ],
                    );
                  },
                ),
                const SizedBox(height: 16),

                // 2. Collapsible Live CCTV Stream for Manager Verification
                if (_showCameraPreview) ...[
                  Card(
                    clipBehavior: Clip.antiAlias,
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Padding(
                          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
                          child: Row(
                            children: [
                              const Icon(Icons.fiber_manual_record, color: Colors.red, size: 14),
                              const SizedBox(width: 6),
                              Text('Live CCTV Feed (YOLO11 Filtered)', style: theme.textTheme.titleSmall),
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
                              child: Text('Waiting for video stream...'),
                            ),
                          ),
                        ),
                      ],
                    ),
                  ),
                  const SizedBox(height: 16),
                ],

                // 3. Interactive Table Floor Plan Cards
                Text('Floor Status by Table', style: theme.textTheme.titleMedium),
                const SizedBox(height: 10),
                GridView.builder(
                  shrinkWrap: true,
                  physics: const NeverScrollableScrollPhysics(),
                  gridDelegate: const SliverGridDelegateWithMaxCrossAxisExtent(
                    maxCrossAxisExtent: 220,
                    mainAxisSpacing: 12,
                    crossAxisSpacing: 12,
                    childAspectRatio: 1.15,
                  ),
                  itemCount: stats.tables.length,
                  itemBuilder: (context, index) {
                    final table = stats.tables.values.elementAt(index);
                    final isOcc = table.occupied;
                    final statusColor = isOcc ? Colors.redAccent : Colors.green;

                    return Card(
                      shape: RoundedRectangleBorder(
                        borderRadius: BorderRadius.circular(12),
                        side: BorderSide(color: statusColor, width: 2),
                      ),
                      color: isOcc ? statusColor.withOpacity(0.10) : null,
                      child: Padding(
                        padding: const EdgeInsets.all(12),
                        child: Column(
                          mainAxisAlignment: MainAxisAlignment.spaceBetween,
                          children: [
                            Row(
                              mainAxisAlignment: MainAxisAlignment.spaceBetween,
                              children: [
                                Text(
                                  table.name,
                                  style: theme.textTheme.titleMedium?.copyWith(fontWeight: FontWeight.bold),
                                ),
                                Container(
                                  padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
                                  decoration: BoxDecoration(
                                    color: statusColor.withOpacity(0.2),
                                    borderRadius: BorderRadius.circular(99),
                                  ),
                                  child: Text(
                                    isOcc ? 'BUSY' : 'FREE',
                                    style: TextStyle(
                                      fontSize: 11,
                                      fontWeight: FontWeight.bold,
                                      color: statusColor,
                                    ),
                                  ),
                                ),
                              ],
                            ),
                            // Visual Seat Dots (shows exactly which chairs are occupied)
                            Row(
                              mainAxisAlignment: MainAxisAlignment.center,
                              children: List.generate(table.totalSeats, (i) {
                                final seatNum = i + 1;
                                final seatTaken = table.takenSeats.contains(seatNum);
                                return Container(
                                  margin: const EdgeInsets.symmetric(horizontal: 4),
                                  width: 16,
                                  height: 16,
                                  decoration: BoxDecoration(
                                    shape: BoxShape.circle,
                                    color: seatTaken ? Colors.redAccent : Colors.grey.withOpacity(0.35),
                                    border: Border.all(
                                      color: seatTaken ? Colors.red : Colors.grey,
                                      width: 1.5,
                                    ),
                                  ),
                                );
                              }),
                            ),
                            Row(
                              mainAxisAlignment: MainAxisAlignment.spaceBetween,
                              children: [
                                Text(
                                  isOcc ? '👥 ${table.guests} Guests' : 'Available',
                                  style: theme.textTheme.bodySmall,
                                ),
                                if (isOcc)
                                  Text(
                                    '⏱ ${_formatDuration(table.durationSec)}',
                                    style: theme.textTheme.bodySmall?.copyWith(fontWeight: FontWeight.w600),
                                  ),
                              ],
                            ),
                          ],
                        ),
                      ),
                    );
                  },
                ),
              ],
            ),
          );
        },
      ),
    );
  }
}

class _KpiCard extends StatelessWidget {
  final String label;
  final String value;
  final IconData icon;
  final Color color;

  const _KpiCard({
    required this.label,
    required this.value,
    required this.icon,
    required this.color,
  });

  @override
  Widget build(BuildContext context) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          mainAxisAlignment: MainAxisAlignment.spaceBetween,
          children: [
            Row(
              mainAxisAlignment: MainAxisAlignment.spaceBetween,
              children: [
                Flexible(
                  child: Text(
                    label,
                    style: Theme.of(context).textTheme.bodySmall,
                    overflow: TextOverflow.ellipsis,
                  ),
                ),
                Icon(icon, size: 18, color: color),
              ],
            ),
            Text(
              value,
              style: Theme.of(context).textTheme.titleLarge?.copyWith(fontWeight: FontWeight.bold),
            ),
          ],
        ),
      ),
    );
  }
}