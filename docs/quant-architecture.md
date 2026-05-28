```mermaid
treeView-beta
  root((Microsoft Qlib AI量化平台))
    1 数据层 Data
      数据源
        A股
        美股
        自定义数据
        高频/分钟线扩展
      数据格式
        Calendar
        Instruments
        Features
        Labels
      数据处理
        Normalize
        Fillna
        Dropna
        Filter
        Processor
      数据接口
        DataHandler
        DatasetH
        DataLoader
        Expression Engine

    2 因子层 Feature / Alpha
      内置因子
        Alpha158
        Alpha360
      自定义因子
        价量因子
        技术指标
        基本面因子
        Level2资金流因子
        DDE大单因子
      标签构造
        未来收益率
        超额收益
        排名标签
        分类标签

    3 模型层 Model
      传统机器学习
        LightGBM
        XGBoost
        CatBoost
        Linear Model
      深度学习
        LSTM
        GRU
        Transformer
        TCN
        ALSTM
      强化学习
        Order Execution
        Portfolio RL
      预测目标
        Alpha预测
        Rank预测
        涨跌概率
        风险收益比

    4 策略层 Strategy
      信号生成
        Pred Score
        Rank IC
        Buy/Sell Signal
      选股策略
        TopK
        DropoutTopK
        EnhancedIndexing
      组合构建
        权重分配
        行业中性
        风格中性
        风险预算
      风控规则
        仓位上限
        单票限制
        换手限制
        止盈止损

    5 回测层 Backtest
      交易模拟
        Order
        Exchange
        Executor
        Account
      成本模型
        手续费
        印花税
        滑点
        冲击成本
      回测指标
        年化收益
        最大回撤
        Sharpe
        IC
        ICIR
        Rank IC
        Turnover

    6 工作流层 Workflow
      qrun
        YAML配置
        自动执行
        研究流水线
      Recorder
        SignalRecord
        SigAnaRecord
        PortAnaRecord
      Experiment
        参数记录
        模型记录
        结果记录
        图表分析

    7 执行层 Execution
      模拟执行
        日频调仓
        分钟级调仓
      订单执行
        目标仓位
        成交模拟
        成本扣除
      实盘扩展
        VN.PY
        CTP
        券商API
        自研OMS

    8 应用场景
      AI选股
      多因子模型
      指数增强
      主力资金识别
      Level2盘口因子
      组合优化
      策略回测
      自动化研究平台

    9 外部系统集成
      数据库
        DolphinDB
        DuckDB
        PostgreSQL
        Parquet
      实验管理
        MLflow
        TensorBoard
      实盘交易
        VN.PY
        CTP
        Broker API
      可视化
        Jupyter
        Dash
        Streamlit
```
