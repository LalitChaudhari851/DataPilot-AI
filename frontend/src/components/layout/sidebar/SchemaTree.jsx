import { memo, useMemo, useEffect, useRef } from 'react';
import { AnimatePresence, motion } from 'framer-motion';
import { ChevronRight, Database, Folder, FolderOpen, SearchX } from 'lucide-react';
import useChatStore from '../../../store/useChatStore';
import { parseSchemaText, groupTables } from './schemaData';
import GroupNode from './GroupNode';
import SchemaSkeletons from './SchemaSkeletons';

const folderVariants = {
  collapsed: { height: 0, opacity: 0 },
  expanded: {
    height: 'auto',
    opacity: 1,
    transition: {
      height: { duration: 0.2, ease: [0.16, 1, 0.3, 1] },
      opacity: { duration: 0.15, delay: 0.04 },
    },
  },
  exit: {
    height: 0,
    opacity: 0,
    transition: { height: { duration: 0.18 }, opacity: { duration: 0.1 } },
  },
};

/**
 * SchemaTree — the complete schema explorer section.
 *
 * Reads schema state directly from Zustand store for performance
 * (avoids re-rendering the entire Sidebar when schema state changes).
 *
 * Features:
 * - Loading skeletons while schema loads
 * - Folder/FolderOpen icons with rotating chevron
 * - Independently collapsible table groups
 * - Auto-expand on search with text highlighting
 * - Empty state when search yields no results
 * - Real column metadata from parsed schema_text
 * - Persisted expanded state across refreshes
 */
const SchemaTree = memo(function SchemaTree({
  searchQuery,
  onTableSelected,
  schemaCollapsed,
  onToggleSchema,
}) {
  // Read schema state from store directly
  const schemaTables = useChatStore(s => s.schemaTables);
  const schemaText = useChatStore(s => s.schemaText);
  const selectedSchema = useChatStore(s => s.selectedSchema);
  const setSelectedSchema = useChatStore(s => s.setSelectedSchema);
  const schemaFolderOpen = useChatStore(s => s.schemaFolderOpen);
  const setSchemaFolderOpen = useChatStore(s => s.setSchemaFolderOpen);
  const expandedGroups = useChatStore(s => s.expandedGroups);
  const setExpandedGroup = useChatStore(s => s.setExpandedGroup);
  const expandedTables = useChatStore(s => s.expandedTables);
  const toggleExpandedTable = useChatStore(s => s.toggleExpandedTable);
  const selectedDbId = useChatStore(s => s.selectedDbId);
  const availableDatabases = useChatStore(s => s.availableDatabases);

  const currentDb = availableDatabases.find(d => d.db_id === selectedDbId) || {
    name: selectedDbId === 'E_commerce' ? 'E-Commerce (Spider)' : 'DataPilot SaaS',
    table_count: schemaTables.length,
  };

  // Parse schema text into structured metadata
  const parsedSchema = useMemo(() => parseSchemaText(schemaText), [schemaText]);

  // Group and filter tables
  const groupedTables = useMemo(
    () => groupTables(schemaTables, searchQuery),
    [schemaTables, searchQuery]
  );

  // Track pre-search folder state for restore
  const preSearchRef = useRef(schemaFolderOpen);

  // Auto-expand folder when searching
  useEffect(() => {
    if (searchQuery) {
      preSearchRef.current = schemaFolderOpen;
      if (!schemaFolderOpen) setSchemaFolderOpen(true);
    } else {
      // Restore previous state when search clears
      if (preSearchRef.current !== schemaFolderOpen && !searchQuery) {
        setSchemaFolderOpen(preSearchRef.current);
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchQuery]);

  const isSearchActive = searchQuery && searchQuery.length > 0;
  const hasResults = groupedTables.length > 0;
  const totalFilteredTables = groupedTables.reduce((sum, [, tables]) => sum + tables.length, 0);
  const isSchemaLoading = schemaTables.length === 0;

  return (
    <>
      {/* Section header — "Schema" */}
      <button
        onClick={onToggleSchema}
        className="flex items-center gap-1.5 px-3 py-2 w-full group mt-4 first:mt-0"
        aria-expanded={!schemaCollapsed}
        aria-label="Schema section"
      >
        <ChevronRight
          size={11}
          className={`text-t4 transition-transform duration-200 ${!schemaCollapsed ? 'rotate-90' : ''}`}
        />
        <Database
          size={12}
          className={`text-t4 transition-colors duration-200 ${!schemaCollapsed ? 'text-t3' : 'group-hover:text-t3'}`}
        />
        <span className="text-[10px] font-bold uppercase tracking-widest text-t4 group-hover:text-t3 transition-colors">
          Schema
        </span>
      </button>

      {/* Collapsible schema content */}
      <AnimatePresence initial={false}>
        {!schemaCollapsed && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
            className="overflow-hidden"
          >
            {/* Loading state */}
            {isSchemaLoading ? (
              <SchemaSkeletons />
            ) : (
              <div className="px-2 pb-1">
                {/* Active Database Context Pill */}
                <div className="mb-2 px-2.5 py-1.5 rounded-lg bg-white/[0.03] border border-white/[0.06] flex items-center justify-between text-[11px]">
                  <div className="flex items-center gap-1.5 min-w-0">
                    <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse flex-shrink-0" />
                    <span className="font-semibold text-white truncate text-[11px]">{currentDb.name}</span>
                  </div>
                  <span className="text-[10px] font-mono text-brand-light bg-brand-dim/30 border border-brand/20 px-1 py-0.2 rounded font-medium flex-shrink-0">
                    {schemaTables.length} tables
                  </span>
                </div>

                {/* Tables folder toggle */}
                <button
                  onClick={() => setSchemaFolderOpen(v => !v)}
                  className="nav-item w-full flex items-center gap-2 px-2.5 py-[7px] text-xs text-left group"
                  role="treeitem"
                  aria-expanded={schemaFolderOpen}
                  aria-label="Tables folder"
                >
                  {schemaFolderOpen ? (
                    <FolderOpen size={14} className="flex-shrink-0 text-brand-light transition-colors duration-200" />
                  ) : (
                    <Folder size={14} className="flex-shrink-0 text-t4 group-hover:text-t3 transition-colors duration-200" />
                  )}
                  <ChevronRight
                    size={11}
                    className={`text-t4 transition-transform duration-200 flex-shrink-0 ${schemaFolderOpen ? 'rotate-90' : ''}`}
                  />
                  <span className={`font-semibold transition-colors duration-200 ${
                    schemaFolderOpen ? 'text-t2' : 'text-t3 group-hover:text-t2'
                  }`}>
                    Tables
                  </span>
                </button>

                {/* Collapsible grouped table list */}
                <AnimatePresence initial={false}>
                  {schemaFolderOpen && (
                    <motion.div
                      key="tables-folder"
                      variants={folderVariants}
                      initial="collapsed"
                      animate="expanded"
                      exit="exit"
                      className="overflow-hidden"
                    >
                      {!hasResults && isSearchActive ? (
                        /* Empty search state */
                        <div className="flex flex-col items-center gap-2 py-6 px-4">
                          <div
                            className="w-8 h-8 rounded-lg flex items-center justify-center"
                            style={{ background: 'var(--surface-2)', border: '1px solid var(--border-1)' }}
                          >
                            <SearchX size={14} className="text-t4" />
                          </div>
                          <div className="text-center">
                            <p className="text-t3 text-[11px] font-medium">No matching tables</p>
                            <p className="text-t4 text-[10px] mt-0.5">Try a different keyword</p>
                          </div>
                        </div>
                      ) : (
                        /* Grouped table list */
                        <div className="mt-1 pl-0.5 pb-1" role="tree" aria-label="Database tables">
                          {groupedTables.map(([groupName, tables]) => (
                            <GroupNode
                              key={groupName}
                              groupName={groupName}
                              tables={tables}
                              expanded={expandedGroups[groupName] !== false}
                              onToggle={setExpandedGroup}
                              selectedSchema={selectedSchema}
                              onSelectSchema={setSelectedSchema}
                              expandedTables={expandedTables}
                              onToggleTable={toggleExpandedTable}
                              parsedSchema={parsedSchema}
                              onTableSelected={onTableSelected}
                              searchQuery={searchQuery}
                            />
                          ))}
                        </div>
                      )}
                    </motion.div>
                  )}
                </AnimatePresence>
              </div>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </>
  );
});

export default SchemaTree;
