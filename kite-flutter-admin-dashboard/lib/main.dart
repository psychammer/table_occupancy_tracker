import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'features/table_pulse/presentation/table_pulse_shell.dart';

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(
    const ProviderScope(
      child: TablePulseKiteApp(),
    ),
  );
}

class TablePulseKiteApp extends StatelessWidget {
  const TablePulseKiteApp({super.key});

  @override
  Widget build(BuildContext context) {
    // Styled with Kite's Shadcn Dark Zinc/Slate design tokens
    return MaterialApp(
      title: 'TablePulse',
      debugShowCheckedModeBanner: false,
      themeMode: ThemeMode.dark,
      darkTheme: ThemeData(
        useMaterial3: true,
        brightness: Brightness.dark,
        scaffoldBackgroundColor: const Color(0xFF09090B), // Kite Zinc-950
        colorScheme: const ColorScheme.dark(
          primary: Color(0xFFFAFAFA),
          onPrimary: Color(0xFF18181B),
          secondary: Color(0xFF27272A),
          surface: Color(0xFF18181B), // Kite Zinc-900 card surface
          onSurface: Color(0xFFFAFAFA),
          error: Color(0xFFEF4444),
        ),
        dividerColor: const Color(0xFF27272A),
        cardTheme: CardThemeData(
          color: const Color(0xFF121215),
          elevation: 0,
          margin: EdgeInsets.zero,
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(10),
            side: const BorderSide(color: Color(0xFF27272A), width: 1),
          ),
        ),
        appBarTheme: const AppBarTheme(
          backgroundColor: Color(0xFF09090B),
          elevation: 0,
          centerTitle: false,
          scrolledUnderElevation: 0,
        ),
        navigationBarTheme: NavigationBarThemeData(
          backgroundColor: const Color(0xFF09090B),
          indicatorColor: const Color(0xFF27272A),
          labelTextStyle: WidgetStateProperty.all(
            const TextStyle(fontSize: 12, fontWeight: FontWeight.w500),
          ),
        ),
      ),
      home: const TablePulseShell(),
    );
  }
}