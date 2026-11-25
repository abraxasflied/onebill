import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:path_provider/path_provider.dart';
import 'package:serious_python/serious_python.dart';
import 'package:webview_flutter/webview_flutter.dart';

const int kPort = 8765;

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(const OneBillApp());
}

class OneBillApp extends StatelessWidget {
  const OneBillApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'OneBill',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        colorScheme: ColorScheme.fromSeed(
          seedColor: const Color(0xFFD4FF3F),
          brightness: Brightness.dark,
        ),
        scaffoldBackgroundColor: const Color(0xFF0B0C08),
        useMaterial3: true,
      ),
      home: const BootScreen(),
    );
  }
}

/// Starts the Python engine, then shows the UI in a WebView with the mobile skin.
class BootScreen extends StatefulWidget {
  const BootScreen({super.key});

  @override
  State<BootScreen> createState() => _BootScreenState();
}

class _BootScreenState extends State<BootScreen> {
  WebViewController? _web;
  String? _error;
  String _css = '';
  String _js = '';

  @override
  void initState() {
    super.initState();
    _boot();
  }

  Future<void> _boot() async {
    try {
      _css = await rootBundle.loadString('assets/mobile/skin.css');
      _js = await rootBundle.loadString('assets/mobile/skin.js');
      final docs = await getApplicationDocumentsDirectory();

      // Starts the engine thread from python/onebill_android_entry.py.
      await SeriousPython.run(
        'app/app.zip',
        appFileName: 'onebill_android_entry.py',
        environmentVariables: {
          'ONEBILL_PORT': '$kPort',
          'ONEBILL_DATA': '${docs.path}/OneBill_Data',
        },
      );
      await _waitForServer();

      final controller = WebViewController()
        ..setJavaScriptMode(JavaScriptMode.unrestricted)
        ..setBackgroundColor(const Color(0xFF0B0C08))
        ..setNavigationDelegate(NavigationDelegate(
          onPageFinished: (_) {
            final w = _web;
            if (w != null) _injectSkin(w);
          },
        ));
      _web = controller;
      await controller.loadRequest(Uri.parse('http://127.0.0.1:$kPort/'));

      if (mounted) setState(() {});
    } catch (e) {
      if (mounted) setState(() => _error = e.toString());
    }
  }

  /// Adds the mobile look and behaviour to the page the engine serves.
  Future<void> _injectSkin(WebViewController c) async {
    try {
      await c.runJavaScript(
        "(function(){var s=document.createElement('style');"
        "s.textContent=${jsonEncode(_css)};document.head.appendChild(s);})();",
      );
      await c.runJavaScript(_js);
    } catch (e) {
      debugPrint('OneBill skin not applied: $e');
    }
  }

  Future<void> _waitForServer() async {
    final client = HttpClient();
    for (var i = 0; i < 60; i++) {
      try {
        final req = await client.getUrl(
            Uri.parse('http://127.0.0.1:$kPort/api/ping'));
        final res = await req.close();
        await res.drain<void>();
        if (res.statusCode == 200) return;
      } catch (_) {/* not ready yet */}
      await Future.delayed(const Duration(milliseconds: 250));
    }
    throw TimeoutException('OneBill engine did not start');
  }

  /// Android Back: close a sheet or go back to Sale in the engine first.
  /// Only when the engine has nothing left to close does the app exit.
  Future<void> _onBack() async {
    final c = _web;
    var handled = false;
    if (c != null) {
      try {
        final r = await c.runJavaScriptReturningResult(
            'window.obBack ? window.obBack() : false');
        handled = r.toString().contains('true');
      } catch (_) {}
    }
    if (!handled) SystemNavigator.pop();
  }

  @override
  Widget build(BuildContext context) {
    Widget body;
    if (_error != null) {
      body = Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Text('Could not start OneBill.\n\n$_error',
              textAlign: TextAlign.center),
        ),
      );
    } else if (_web == null) {
      body = const Center(child: CircularProgressIndicator());
    } else {
      body = SafeArea(child: WebViewWidget(controller: _web!));
    }

    return PopScope(
      canPop: false,
      onPopInvokedWithResult: (didPop, result) {
        if (!didPop) _onBack();
      },
      child: Scaffold(body: body),
    );
  }
}
