-- =============================================================================
-- Synthetic Source Database: SQL Server 2022 (sql-src-01 / retail_demo)
-- Domain: Generic E-Commerce Retail OLTP (Customers, Products, Orders, OrderItems, Payments)
-- =============================================================================

IF DB_ID(N'retail_demo') IS NULL
BEGIN
    CREATE DATABASE retail_demo;
END
GO

USE retail_demo;
GO

SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
GO

-- -----------------------------------------------------------------------------
-- Tables
-- -----------------------------------------------------------------------------
CREATE TABLE dbo.Customers (
    CustomerID    BIGINT IDENTITY(1,1) NOT NULL,
    EmailAddress  NVARCHAR(255)        NOT NULL,
    FullName      NVARCHAR(150)        NOT NULL,
    VATNumber     VARCHAR(32)          NULL,
    LoyaltyTier   VARCHAR(20)          NOT NULL CONSTRAINT DF_Customers_LoyaltyTier DEFAULT ('STANDARD'),
    CreatedAtUTC  DATETIME2(6)         NOT NULL CONSTRAINT DF_Customers_CreatedAtUTC DEFAULT (SYSUTCDATETIME()),
    CONSTRAINT PK_Customers PRIMARY KEY CLUSTERED (CustomerID),
    CONSTRAINT UQ_Customers_EmailAddress UNIQUE (EmailAddress)
);
GO

CREATE TABLE dbo.Products (
    ProductID     BIGINT IDENTITY(1,1) NOT NULL,
    SKUCode       VARCHAR(64)          NOT NULL,
    ProductName   NVARCHAR(200)        NOT NULL,
    UnitPrice     DECIMAL(18,2)        NOT NULL,
    StockQty      INT                  NOT NULL,
    IsActive      BIT                  NOT NULL CONSTRAINT DF_Products_IsActive DEFAULT (1),
    CONSTRAINT PK_Products PRIMARY KEY CLUSTERED (ProductID),
    CONSTRAINT UQ_Products_SKUCode UNIQUE (SKUCode)
);
GO

CREATE TABLE dbo.Orders (
    OrderID          BIGINT IDENTITY(1,1) NOT NULL,
    CustomerID       BIGINT               NOT NULL,
    OrderDateUTC     DATETIME2(6)         NOT NULL,
    TotalAmount      DECIMAL(18,2)        NOT NULL,
    VATAmount        DECIMAL(18,2)        NOT NULL,
    OrderStatus      VARCHAR(30)          NOT NULL,
    PromoChannelCode VARCHAR(32)          NULL,
    CONSTRAINT PK_Orders PRIMARY KEY CLUSTERED (OrderID),
    CONSTRAINT FK_Orders_Customers FOREIGN KEY (CustomerID) REFERENCES dbo.Customers (CustomerID)
);
GO

CREATE NONCLUSTERED INDEX IX_Orders_CustomerID_OrderDateUTC
    ON dbo.Orders (CustomerID, OrderDateUTC);
GO

CREATE TABLE dbo.OrderItems (
    OrderItemID   BIGINT IDENTITY(1,1) NOT NULL,
    OrderID       BIGINT               NOT NULL,
    ProductID     BIGINT               NOT NULL,
    Quantity      INT                  NOT NULL,
    LineAmount    DECIMAL(18,2)        NOT NULL,
    CONSTRAINT PK_OrderItems PRIMARY KEY CLUSTERED (OrderItemID),
    CONSTRAINT FK_OrderItems_Orders   FOREIGN KEY (OrderID)   REFERENCES dbo.Orders (OrderID),
    CONSTRAINT FK_OrderItems_Products FOREIGN KEY (ProductID) REFERENCES dbo.Products (ProductID)
);
GO

CREATE NONCLUSTERED INDEX IX_OrderItems_OrderID
    ON dbo.OrderItems (OrderID);
GO

CREATE TABLE dbo.Payments (
    PaymentID       BIGINT IDENTITY(1,1) NOT NULL,
    OrderID         BIGINT               NOT NULL,
    PaymentRefCode  VARCHAR(64)          NOT NULL,
    PaidAmount      DECIMAL(18,2)        NOT NULL,
    PaidAtUTC       DATETIME2(6)         NOT NULL,
    PaymentMethod   VARCHAR(32)          NOT NULL,
    CONSTRAINT PK_Payments PRIMARY KEY CLUSTERED (PaymentID),
    CONSTRAINT FK_Payments_Orders FOREIGN KEY (OrderID) REFERENCES dbo.Orders (OrderID),
    CONSTRAINT UQ_Payments_PaymentRefCode UNIQUE (PaymentRefCode)
);
GO

CREATE TABLE dbo.LegacyAuditLog (
    AuditEventID  BIGINT IDENTITY(1,1) NOT NULL,
    EntityName    VARCHAR(64)          NOT NULL,
    EventPayload  NVARCHAR(MAX)        NULL,
    LoggedAtUTC   DATETIME2(6)         NOT NULL,
    CONSTRAINT PK_LegacyAuditLog PRIMARY KEY CLUSTERED (AuditEventID)
);
GO

-- -----------------------------------------------------------------------------
-- Synthetic Seed Data
-- -----------------------------------------------------------------------------
INSERT INTO dbo.Customers (EmailAddress, FullName, VATNumber, LoyaltyTier, CreatedAtUTC)
VALUES
    (N'alice.morgan@example.org', N'Alice Morgan', 'EU100200301', 'GOLD',     '2026-01-10T08:00:00'),
    (N'ben.carter@example.org',   N'Ben Carter',   'EU100200302', 'SILVER',   '2026-01-12T09:15:00'),
    (N'claire.wu@example.org',    N'Claire Wu',    NULL,          'STANDARD', '2026-01-15T11:30:00'),
    (N'david.patel@example.org',  N'David Patel',  'EU100200304', 'GOLD',     '2026-01-20T14:45:00');
GO

INSERT INTO dbo.Products (SKUCode, ProductName, UnitPrice, StockQty, IsActive)
VALUES
    ('SKU-KB-001',  N'Mechanical Keyboard Pro', 120.00, 50, 1),
    ('SKU-MS-002',  N'Ergonomic Wireless Mouse', 45.50, 120, 1),
    ('SKU-MON-003', N'27-Inch 4K IPS Monitor',  380.00, 30, 1),
    ('SKU-HUB-004', N'USB-C Docking Hub',        65.00, 85, 1);
GO

INSERT INTO dbo.Orders (CustomerID, OrderDateUTC, TotalAmount, VATAmount, OrderStatus, PromoChannelCode)
VALUES
    (1, '2026-02-01T10:00:00', 182.05, 16.55, 'PAID',      'WEB_DIRECT'),
    (2, '2026-02-02T12:30:00', 418.00, 38.00, 'PAID',      'PARTNER_API'),
    (3, '2026-02-03T15:20:00', 143.00, 13.00, 'SHIPPED',   NULL),
    (4, '2026-02-04T16:40:00', 132.00, 12.00, 'PAID',      'MOBILE_APP');
GO

INSERT INTO dbo.OrderItems (OrderID, ProductID, Quantity, LineAmount)
VALUES
    (1, 1, 1, 120.00),
    (1, 2, 1,  45.50),
    (2, 3, 1, 380.00),
    (3, 4, 2, 130.00),
    (4, 1, 1, 120.00);
GO

INSERT INTO dbo.Payments (OrderID, PaymentRefCode, PaidAmount, PaidAtUTC, PaymentMethod)
VALUES
    (1, 'PAY-2026-0001', 182.05, '2026-02-01T10:01:00', 'CARD'),
    (2, 'PAY-2026-0002', 418.00, '2026-02-02T12:32:00', 'BANK_TRANSFER'),
    (3, 'PAY-2026-0003', 143.00, '2026-02-03T15:25:00', 'CARD'),
    (4, 'PAY-2026-0004', 132.00, '2026-02-04T16:41:00', 'WALLET');
GO

INSERT INTO dbo.LegacyAuditLog (EntityName, EventPayload, LoggedAtUTC)
VALUES
    ('Orders', N'{"event":"created","order_id":1}', '2026-02-01T10:00:01'),
    ('Payments', N'{"event":"settled","payment_id":1}', '2026-02-01T10:01:05');
GO

-- -----------------------------------------------------------------------------
-- Stored Procedures
-- -----------------------------------------------------------------------------
CREATE OR ALTER PROCEDURE dbo.usp_GetCustomerOrders
    @CustomerID BIGINT
AS
BEGIN
    SET NOCOUNT ON;
    SELECT OrderID, OrderDateUTC, TotalAmount, VATAmount, OrderStatus
    FROM dbo.Orders
    WHERE CustomerID = @CustomerID
    ORDER BY OrderDateUTC DESC;
END
GO

CREATE OR ALTER PROCEDURE dbo.usp_CalculateOrderVAT
    @OrderID BIGINT,
    @VATRate DECIMAL(5,4)
AS
BEGIN
    SET NOCOUNT ON;
    SELECT OrderID, ROUND(TotalAmount * @VATRate, 2) AS EstimatedVAT
    FROM dbo.Orders
    WHERE OrderID = @OrderID;
END
GO

CREATE OR ALTER PROCEDURE dbo.usp_ArchiveOldOrders
    @CutoffDateUTC DATETIME2(6)
AS
BEGIN
    SET NOCOUNT ON;
    SELECT COUNT(*) AS ArchivableCount
    FROM dbo.Orders
    WHERE OrderDateUTC < @CutoffDateUTC;
END
GO

CREATE OR ALTER PROCEDURE dbo.usp_PurgeTempStaging
AS
BEGIN
    SET NOCOUNT ON;
    SELECT 0 AS PurgedRows;
END
GO

-- -----------------------------------------------------------------------------
-- Least-Privilege Roles & Grants
-- -----------------------------------------------------------------------------
IF DATABASE_PRINCIPAL_ID(N'app_retail_rw') IS NULL
BEGIN
    CREATE ROLE app_retail_rw;
END
IF DATABASE_PRINCIPAL_ID(N'bi_readonly') IS NULL
BEGIN
    CREATE ROLE bi_readonly;
END
GO

GRANT SELECT, INSERT, UPDATE ON dbo.Customers  TO app_retail_rw;
GRANT SELECT, INSERT, UPDATE ON dbo.Orders     TO app_retail_rw;
GRANT SELECT, INSERT, UPDATE ON dbo.Payments   TO app_retail_rw;
GRANT SELECT                 ON dbo.Orders     TO bi_readonly;
GRANT SELECT                 ON dbo.OrderItems TO bi_readonly;
GRANT EXECUTE                ON dbo.usp_GetCustomerOrders TO app_retail_rw;
GO
