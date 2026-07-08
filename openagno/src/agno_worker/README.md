基于 Agno SDK 与Mysql数据库，构建通用型 Agent 系统，需要实现以下功能：  

1. 动态钩子函数系统
  设计并实现以下钩子函数接口：  
   a) Prompt 动态组装钩子：  
      - get_system_prompt_hook(run_context, session_state) -> str  
      - get_instructions_hook(run_context, user_profile) -> str  
      - get_context_filter_hook(run_context) -> Dict  
   b) MCP 动态连接钩子：  
      - get_mcp_servers_hook(run_context, business_scenario) -> List[MCPServerConfig]  
      - mcp_tool_filter_hook(run_context, available_tools) -> List[Tool]  
      - mcp_connection_hook(server_config) -> None  
   c) Skills 动态加载钩子：  
      - get_skills_hook(run_context, user_requirements) -> List[Skill]  
      - skill_instruction_hook(skill_name, run_context) -> str  
      - skill_script_hook(script_name, run_context) -> Any  
   d) 数据源钩子：  
      - get_db_connection_hook(run_context) -> DBConnection  
      - data_query_hook(query, run_context) -> str  
      - result_processing_hook(results, run_context) -> Dict  
   e) 会话管理钩子：  
      - session_init_hook(session_id, user_context) -> None  
      - session_update_hook(session_state, run_context) -> Dict  
      - session_cleanup_hook(session_id) -> None
2. 钩子函数加载机制
  - 当前先完成功能开发，最终需要做成镜像，因此这些函数最终需要能够通过 PVC 挂载出来，以实现动态加载 Python 模块
3. 动态 Agent 构建器
  - 基于钩子函数返回值动态配置 Agent 实例  
  - 实时更新 tools、instructions、knowledge 等组件
4. 执行流程集成
  实现 Agent 执行流程：  
   用户请求 → pre_hooks执行 → 动态指令生成 → MCP工具动态加载 →  
   Skills动态加载 → Agent执行 → post_hooks执行 → 响应返回
5. Docker 部署支持
  - 设计 Docker 镜像结构  
  - 配置 PVC 挂载点用于钩子函数代码  
  - 支持环境变量配置钩子函数目录  
  - 实现健康检查和监控端点

技术要求：  

- 使用 Agno SDK 的 Agent、pre_hooks、post_hooks 机制  
- 利用 callable instructions 实现动态提示词  
- 使用 MCPTools 集成外部 MCP 服务器  
- 利用 Skills 系统提供领域专业知识  
- 实现自定义 knowledge retriever 连接 MySQL  
- 遵循 Agno 的 RunContext 和会话管理模式

请设计完整的架构方案，包括模块划分、接口定义、数据流设计，以及错误处理。  