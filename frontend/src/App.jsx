import { useEffect } from 'react';
import { AnimatePresence, motion } from 'framer-motion';
import BootScreen from './components/boot/BootScreen';
import AppShell from './components/layout/AppShell';
import useChatStore from './store/useChatStore';
import { fetchHealth, fetchSchema, fetchDatabases } from './api/client';

export default function App() {
  const booted = useChatStore(s => s.booted);
  const setHealth = useChatStore(s => s.setHealth);
  const selectedDbId = useChatStore(s => s.selectedDbId);
  const setAvailableDatabases = useChatStore(s => s.setAvailableDatabases);
  const setSchemaTables = useChatStore(s => s.setSchemaTables);
  const setSchemaText = useChatStore(s => s.setSchemaText);

  // Poll health every 30 seconds after boot
  useEffect(() => {
    if (!booted) return;
    const poll = async () => {
      const t0 = performance.now();
      try {
        const h = await fetchHealth();
        setHealth({ status: h.status === 'healthy' ? 'healthy' : 'degraded', latency: Math.round(performance.now() - t0) });
      } catch {
        setHealth({ status: 'offline', latency: null });
      }
    };
    poll();
    const id = setInterval(poll, 30_000);
    return () => clearInterval(id);
  }, [booted, setHealth]);

  // Load databases on boot
  useEffect(() => {
    if (!booted) return;
    fetchDatabases()
      .then(res => {
        if (res?.databases?.length) setAvailableDatabases(res.databases);
      })
      .catch(() => {});
  }, [booted, setAvailableDatabases]);

  // Load schema whenever active database (selectedDbId) changes
  useEffect(() => {
    if (!booted) return;
    fetchSchema(selectedDbId)
      .then(d => {
        if (d.tables) setSchemaTables(d.tables);
        if (d.schema_text) setSchemaText(d.schema_text);
      })
      .catch(() => {});
  }, [booted, selectedDbId, setSchemaTables, setSchemaText]);

  return (
    <div className="h-full w-full overflow-hidden">
      <AnimatePresence mode="wait">
        {!booted
          ? <BootScreen key="boot" />
          : (
            <motion.div
              key="app"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              transition={{ duration: 0.4 }}
              className="h-full w-full"
            >
              <AppShell />
            </motion.div>
          )
        }
      </AnimatePresence>
    </div>
  );
}
