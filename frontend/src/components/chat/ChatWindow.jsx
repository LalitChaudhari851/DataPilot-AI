import { useEffect, useRef, useState, useCallback } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { ArrowDown } from 'lucide-react';
import MessageBubble from './MessageBubble';
import WelcomeScreen from './WelcomeScreen';
import useChatStore from '../../store/useChatStore';

export default function ChatWindow({ onPrompt, onRegenerate }) {
  const chat = useChatStore(s => s.getActiveChat());
  const bottomRef = useRef(null);
  const scrollRef = useRef(null);
  const [showScrollBtn, setShowScrollBtn] = useState(false);
  const messages = chat?.messages ?? [];

  // Reset to top when conversation is empty (New Analysis)
  useEffect(() => {
    if (messages.length === 0 && scrollRef.current) {
      scrollRef.current.scrollTop = 0;
    }
  }, [messages.length, chat?.id]);

  // Auto-scroll to bottom only when messages exist and are added or streaming
  useEffect(() => {
    if (messages.length > 0) {
      bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
    }
  }, [messages.length, messages[messages.length - 1]?.streaming]);

  // Show scroll-to-bottom button when user scrolls up
  const handleScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const distFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    setShowScrollBtn(distFromBottom > 200);
  }, []);

  const scrollToBottom = useCallback(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, []);

  return (
    <div
      ref={scrollRef}
      onScroll={handleScroll}
      className="flex-1 overflow-y-auto scrollbar-none relative"
      id="chatScroll"
    >
      <div className="mx-auto min-h-full max-w-4xl px-4 py-8">
        {messages.length === 0 ? (
          <WelcomeScreen onPrompt={onPrompt} />
        ) : (
          <div className="space-y-6">
            <AnimatePresence initial={false}>
              {messages.map((msg) => (
                <motion.div
                  key={msg.id}
                  initial={{ opacity: 0, y: 12 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ duration: 0.3, ease: [0.16, 1, 0.3, 1] }}
                >
                  <MessageBubble
                    message={msg}
                    chatId={chat.id}
                    onRegenerate={() => onRegenerate(msg.id)}
                  />
                </motion.div>
              ))}
            </AnimatePresence>
          </div>
        )}
        <div ref={bottomRef} className="h-10" />
      </div>

      {/* Scroll-to-bottom FAB */}
      <AnimatePresence>
        {showScrollBtn && messages.length > 0 && (
          <motion.button
            initial={{ opacity: 0, scale: 0.8, y: 10 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.8, y: 10 }}
            onClick={scrollToBottom}
            className="fixed bottom-36 right-8 z-20 w-10 h-10 rounded-full flex items-center justify-center shadow-xl border-glow focus-ring"
            style={{
              background: 'var(--surface-3)',
              border: '1px solid var(--border-2)',
              backdropFilter: 'blur(16px)',
            }}
            whileHover={{ scale: 1.05 }}
            whileTap={{ scale: 0.95 }}
            aria-label="Scroll to bottom"
          >
            <ArrowDown size={15} className="text-t2" />
          </motion.button>
        )}
      </AnimatePresence>
    </div>
  );
}
