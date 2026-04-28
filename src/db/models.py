from sqlalchemy import Column, Integer, String, Text, ForeignKey, JSON, Numeric, Boolean, DateTime, Index
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from .database import Base

# ==========================================
# Legal Tree Models
# ==========================================

class Node(Base):
    __tablename__ = "nodes"

    id = Column(Integer, primary_key=True, index=True)
    source = Column(Text, nullable=False)  # 'federal_register', 'uscis_manual', 'ecfr', or 'ina'
    parent_id = Column(Integer, ForeignKey("nodes.id"), nullable=True, index=True)
    level = Column(Integer, nullable=False)
    title = Column(Text, nullable=False)
    summary = Column(Text, nullable=True)
    full_text = Column(Text, nullable=True)
    citation = Column(Text, nullable=True)
    metadata_ = Column("metadata", JSON, nullable=True)

    # Relationships
    parent = relationship("Node", remote_side=[id], backref="children")
    cross_references_out = relationship("NodeCrossReference", foreign_keys="[NodeCrossReference.source_node_id]", back_populates="source_node")
    cross_references_in = relationship("NodeCrossReference", foreign_keys="[NodeCrossReference.target_node_id]", back_populates="target_node")


class NodeCrossReference(Base):
    __tablename__ = "node_cross_references"

    id = Column(Integer, primary_key=True, index=True)
    source_node_id = Column(Integer, ForeignKey("nodes.id"))
    target_node_id = Column(Integer, ForeignKey("nodes.id"))
    reference_text = Column(Text)

    source_node = relationship("Node", foreign_keys=[source_node_id], back_populates="cross_references_out")
    target_node = relationship("Node", foreign_keys=[target_node_id], back_populates="cross_references_in")

# ==========================================
# SOC / O*NET Models
# ==========================================

class SOCHierarchy(Base):
    __tablename__ = "soc_hierarchy"

    soc_code = Column(String(10), primary_key=True, index=True)
    title = Column(Text, nullable=False)
    description = Column(Text)
    parent_soc_code = Column(String(10), ForeignKey("soc_hierarchy.soc_code"), nullable=True)
    level = Column(Integer, nullable=False)  # 0=Major, 1=Minor, 2=Broad, 3=Detailed

    parent = relationship("SOCHierarchy", remote_side=[soc_code], backref="children")
    tasks = relationship("ONetTaskStatement", back_populates="soc")
    tools = relationship("ONetToolTechnology", back_populates="soc")


class ONetTaskStatement(Base):
    __tablename__ = "onet_task_statements"

    id = Column(Integer, primary_key=True, index=True)
    soc_code = Column(String(10), ForeignKey("soc_hierarchy.soc_code"))
    task = Column(Text)
    task_type = Column(String(20))

    soc = relationship("SOCHierarchy", back_populates="tasks")


class ONetToolTechnology(Base):
    __tablename__ = "onet_tools_technology"

    id = Column(Integer, primary_key=True, index=True)
    soc_code = Column(String(10), ForeignKey("soc_hierarchy.soc_code"))
    t2_type = Column(String(20))
    t2_example = Column(Text)
    hot_technology = Column(Boolean, default=False)

    soc = relationship("SOCHierarchy", back_populates="tools")

# ==========================================
# Wage & MSA Mapping
# ==========================================

class SOCCrosswalk(Base):
    __tablename__ = "soc_crosswalk"

    id = Column(Integer, primary_key=True, index=True)
    oflc_soc_code = Column(String(10), index=True)
    onet_soc_code = Column(String(10), ForeignKey("soc_hierarchy.soc_code"))
    mapping_type = Column(Text)  # 'exact', 'merged', 'split'


class OFLCWage(Base):
    __tablename__ = "oflc_wages"
    __table_args__ = (
        Index('ix_oflc_wages_soc_msa', 'soc_code', 'msa_area'),
    )

    id = Column(Integer, primary_key=True, index=True)
    soc_code = Column(String(10), index=True)  # Raw OFLC code
    soc_title = Column(Text)
    msa_area = Column(Text, index=True)
    wage_level = Column(Integer)
    hourly_wage = Column(Numeric)
    yearly_wage = Column(Numeric)


class MSAMapping(Base):
    __tablename__ = "msa_mapping"

    id = Column(Integer, primary_key=True, index=True)
    zip_code = Column(String(10), index=True)
    city_name = Column(Text, index=True)
    state_abbr = Column(String(2))
    msa_area = Column(Text)

# ==========================================
# Auditing
# ==========================================

class ToolAuditLog(Base):
    __tablename__ = "tool_audit_log"

    id = Column(Integer, primary_key=True, index=True)
    tool_name = Column(Text, nullable=False)
    parameters = Column(JSON, nullable=True)
    called_at = Column(DateTime(timezone=True), server_default=func.now())
