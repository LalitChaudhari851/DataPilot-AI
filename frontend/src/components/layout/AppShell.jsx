import { useState, useEffect, useCallback } from 'react';
import Sidebar from './Sidebar';
import Topbar from './Topbar';
import ChatWindow from '../chat/ChatWindow';
import Composer from '../chat/Composer';
import Toast from '../ui/Toast';
import useChatStore from '../../store/useChatStore';
import { detectConversational, getConversationalResponse, streamChat, fetchSchema } from '../../api/client';


const STAGE_TO_STEP = {
  'classifying': 0, 'Understanding': 0,
  'retrieving': 0, 'Retrieving': 0,
  'Generating': 1, 'SQL': 1, 'Regenerating': 1,
  'Validating': 2, 'Validation': 2,
  'Executing': 3, 'Execution': 3,
  'Preparing': 4, 'response': 4, 'Generating insights': 4,
};

export default function AppShell() {
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const store = useChatStore();
  const { addUserMessage, updateMessage, setIsSending, addToast, setSchemaTables, pushContext } = store;


  // Global keyboard shortcuts
  useEffect(() => {
    const handler = (e) => {
      // Ctrl+N or Cmd+N — new chat
      if ((e.ctrlKey || e.metaKey) && e.key === 'n') {
        e.preventDefault();
        store.newChat();
      }
      // Ctrl+/ — focus composer
      if ((e.ctrlKey || e.metaKey) && e.key === '/') {
        e.preventDefault();
        document.querySelector('#composer-input')?.focus();
      }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [store]);

  const handleSubmit = useCallback(async (question) => {
    if (!question?.trim() || store.isSending) return;

    const { chat, pendingId } = addUserMessage(question);
    setIsSending(true);

    // ── Conversational fast-path (client-side) ─────────────
    const intentType = detectConversational(question);
    if (intentType) {
      updateMessage(chat.id, pendingId, { _chatMode: true });
      const response = getConversationalResponse(intentType);
      const tokens = response.split(/(\s+)/);
      let text = '';
      for (const token of tokens) {
        text += token;
        updateMessage(chat.id, pendingId, { pending: false, streaming: true, streamText: text, data: { message: text } });
        await new Promise(r => setTimeout(r, token.trim() ? 22 : 8));
      }
      updateMessage(chat.id, pendingId, { pending: false, streaming: false, streamText: text, data: { message: text } });
      setIsSending(false);
      return;
    }

    // ── Full AI pipeline (SQL mode) ─────────────────────────
    const activeDbId = store.selectedDbId || 'default';
    const assembled = { intent: '', sql: '', explanation: '', answer: [], message: '', insights: [], follow_ups: [], row_count: 0, execution_time_ms: 0, db_id: activeDbId };
    let _accumulatedSql = '';
    let _accumulatedText = '';
    let _currentStep = 0;

    updateMessage(chat.id, pendingId, { _dbId: activeDbId });

    try {
      await streamChat({
        question: (store.selectedSchema && store.selectedSchema !== 'default') 
          ? `${question}\n\nUse table: ${store.selectedSchema}` 
          : question,
        db_id: activeDbId,
        history: (store.getActiveChat()?.context ?? []).slice(-6),
        onChunk: (type, chunk) => {
          switch (type) {
            case 'stage': {
              const stepKey = Object.keys(STAGE_TO_STEP).find(k => chunk.message?.includes(k));
              _currentStep = stepKey ? STAGE_TO_STEP[stepKey] : _currentStep;
              updateMessage(chat.id, pendingId, { _stageText: chunk.message, _pipelineStep: _currentStep, pending: true, streaming: true, _dbId: activeDbId });
              break;
            }
            case 'token':
              if (chunk.token) {
                if (_currentStep <= 1 && assembled.intent !== '') {
                  _accumulatedSql += chunk.token;
                  updateMessage(chat.id, pendingId, { _streamingSql: _accumulatedSql, streaming: true, pending: false, _pipelineStep: 1 });
                } else if (_currentStep >= 3) {
                  _accumulatedText += chunk.token;
                  updateMessage(chat.id, pendingId, { streamText: _accumulatedText, streaming: true, pending: false });
                }
              }
              break;
            case 'summary_token':
              // Progressive summary streaming — tokens arrive as LLM generates
              if (chunk.token) {
                _accumulatedText += chunk.token;
                _currentStep = 4;
                updateMessage(chat.id, pendingId, {
                  streamText: _accumulatedText,
                  streaming: true,
                  pending: false,
                  _pipelineStep: 4,
                });
              }
              break;
            case 'intent':
              assembled.intent = chunk.intent ?? '';
              _currentStep = 1;
              updateMessage(chat.id, pendingId, { _pipelineStep: 1, pending: false, streaming: true });
              break;
            case 'sql':
              assembled.sql = chunk.sql ?? _accumulatedSql;
              assembled.explanation = chunk.explanation ?? '';
              _currentStep = 2;
              updateMessage(chat.id, pendingId, { 
                data: { ...assembled }, 
                _streamingSql: assembled.sql,
                _pipelineStep: 2,
                streaming: true 
              });
              break;
            case 'results':
              assembled.answer = chunk.data ?? [];
              assembled.row_count = chunk.row_count ?? 0;
              assembled.execution_time_ms = chunk.execution_time_ms ?? 0;
              _currentStep = 3;
              updateMessage(chat.id, pendingId, { data: { ...assembled, db_id: activeDbId }, _pipelineStep: 3, streaming: true, _dbId: activeDbId });
              break;
            case 'message':
              assembled.message = chunk.message ?? _accumulatedText;
              assembled.insights = chunk.insights ?? [];
              assembled.follow_ups = chunk.follow_ups ?? [];
              _currentStep = 4;
              updateMessage(chat.id, pendingId, { 
                data: { ...assembled, db_id: activeDbId }, 
                streamText: assembled.message || _accumulatedText, 
                streaming: false, 
                pending: false,
                _pipelineStep: 5,
                _dbId: activeDbId,
              });
              break;
            case 'error':
              updateMessage(chat.id, pendingId, { pending: false, streaming: false, error: chunk.error ?? 'An error occurred.' });
              break;
            case 'done':
              assembled.execution_time_ms = assembled.execution_time_ms || chunk.total_time_ms || 0;
              break;
          }
        }
      });

      if (assembled.sql) {
        pushContext(chat.id, { user: question, sql: assembled.sql });
        addToast('Query complete', 'success');
      }
    } catch (err) {
      const msg = err.message === 'Failed to fetch'
        ? 'Cannot reach the server. Make sure it\'s running.'
        : err.message;
      updateMessage(chat.id, pendingId, { pending: false, streaming: false, error: msg });
      addToast('Query failed', 'error');
    } finally {
      setIsSending(false);
    }
  }, [store, addUserMessage, updateMessage, setIsSending, addToast, pushContext]);

  // Listen for sidebar saved-query shortcuts
  useEffect(() => {
    const handler = (e) => handleSubmit(e.detail.query);
    window.addEventListener('plainsql:submit', handler);
    return () => window.removeEventListener('plainsql:submit', handler);
  }, [handleSubmit]);

  const handleRegenerate = useCallback((msgId) => {
    const chat = store.getActiveChat();
    if (!chat) return;
    const idx = chat.messages.findIndex(m => m.id === msgId);
    if (idx < 1) return;
    const userMsg = chat.messages[idx - 1];
    if (userMsg?.role !== 'user') return;
    // Remove old assistant message and re-submit
    useChatStore.setState(s => ({
      chats: s.chats.map(c => c.id !== chat.id ? c : { ...c, messages: c.messages.filter(m => m.id !== msgId) })
    }));
    handleSubmit(userMsg.content);
  }, [store, handleSubmit]);

  return (
    <div className="flex h-full overflow-hidden" style={{ background: 'var(--surface-0)' }}>
      {/* Subtle background gradient */}
      <div className="fixed inset-0 pointer-events-none bg-grid opacity-60" />
      <div className="fixed top-0 left-0 w-full h-full pointer-events-none"
        style={{ background: 'radial-gradient(ellipse 80% 50% at 50% -20%, rgba(59,130,246,0.06) 0%, transparent 60%)' }} />

      <Sidebar open={sidebarOpen} onClose={() => setSidebarOpen(false)} />

      <div className="flex flex-col flex-1 min-w-0 relative z-10">
        <Topbar onMenuClick={() => setSidebarOpen(true)} />
        <ChatWindow onPrompt={handleSubmit} onRegenerate={handleRegenerate} />
        <Composer onSubmit={handleSubmit} />
      </div>

      <Toast />
    </div>
  );
}
