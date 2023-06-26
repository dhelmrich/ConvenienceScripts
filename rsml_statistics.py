import numpy as np
from numpy.linalg import norm
import scipy
from xml.dom import minidom
import xml.etree.ElementTree as ET
import math
import os
import sys
import argparse


class Organ :
  def __init__(self,Nodes = [], Diameters = [], Functions = {},OrganNumber = 0, Suborgans = [], Pred = -1, PaNo = -1) :
    self = self
    self.Nodes = Nodes
    self.Diameters = Diameters
    self.Functions = Functions
    self.OrganNumber = OrganNumber
    self.Predecessor = Pred
    self.ParentNode = PaNo
    self.Suborgans = Suborgans
    self.orig = OrganNumber
  #end def __init__

  def PushNode(self,pt, params) :
    self.Nodes.append(pt)
    for name,param in params.items() :
      if self.Functions.get(name) != None :
        self.Functions.get(name).append(param)
      else :
        self.Functions[name] = [param]
  #enddef

  def Addorgan(self,organ : int) :
    self.Suborgans.append(organ)
    #endif
  #enddef Addorgan

  def PushPointOnly(self,pt) :
    self.Nodes.appen(pt)
    for name,param in params :
      param.append(0.0) # todo i need default value
    #endfor
  #enddef
  def __str__(self) :
    return "Organ Number "+str(self.OrganNumber)+"! Points: " + str(len(self.Nodes)) \
      + "\n\tFunctions: " + str(len(self.Functions)) \
      + "\n\tParent: " + str(self.Predecessor)

  def __len__(self) :
    return len(self.Nodes)
  #endif

  def copy(self) :
    #def __init__(self,Nodes = [], Diameters = [], Functions = {},OrganNumber = 0, Suborgans = [], Pred = -1, PaNo = -1) :
    return Organ(self.Nodes.copy(),[].copy(),{"diameter":self.Functions["diameter"].copy()}.copy(),self.OrganNumber,self.Suborgans.copy(),self.Predecessor,self.ParentNode)
  #enddef
#end class

def TryParseFloat(splitline : list) :
  try :
    f = float(splitline[0])
    return True
  except :
    return False

def TryParseInt(splitline : list) :
  try :
    f = int(splitline[0])
    return True
  except :
    return False


class ParseRSML :
  def __init__(self) :
    self.fname = ""
  #enddef
  def ParseFile(self) :
    rsmldoc = ET.parse(self.fname)
    rsml = rsmldoc.getroot()
    if rsml.find("metadata") != None and rsml.find("metadata").find("software") != None :
      self.addconnector = True
    else :
      self.addconnector = False
    scene = rsml.find("scene").find("plant")
    toporgans = scene.findall(".//root")
    return self.IncurseOrgans(scene.find('.//root'))
    #endfor
  #enddef
  def IncurseOrgans(self, organelement) :
    organlist = []
    elementlist = [[organelement,-1]]
    while len(elementlist) > 0 :
      parentelementpair = elementlist[0]
      element = parentelementpair[0]
      rn = parentelementpair[1]
      elementlist = elementlist[1:]
      parentnode = int(element.find("properties").find("parent-node").get("value")) if element.find("properties").find("parent-node") != None else -1
      pl = element.find("geometry").find("polyline")
      functionnames = [f.get("name") \
        for f in (element.find("functions").findall("function"))]
      functions = [[float(v.get("value")) for v in f.findall("sample")] \
        for f in (element.find("functions").findall("function"))]
      propertynames = [ e.tag for e in element.find("properties").iterfind(".//") ]
      points = []
      params = {}
      for i,name in enumerate(functionnames) :
        params[name] = functions[i]
      pointstring = ".//Point"
      if pl.find(pointstring) == None :
        pointstring = ".//point"
      for i,ptel in enumerate(pl.findall(pointstring)) :
        point = []
        if ptel.get("X") == None :
          Z = float(ptel.get("z")) if ptel.get("z") != None else 0.0
          point = [float(ptel.get("x")),
          float(ptel.get("y")),
          Z]
        else :
          Z = float(ptel.get("Z")) if ptel.get("Z") != None else 0.0
          point = [float(ptel.get("X")),
          float(ptel.get("Y")),
          Z]
        points.append(point)
      #endfor
      if "diameter" in propertynames :
        d = float(element.find("properties").find("diameter").get("value"))
        params["diameter"] = np.array([d for p in points])
      if self.addconnector and parentnode >= 0 :
        # add an additional node to connect the laterals with their predecessors
        parentorgan = next(r for r in organlist if r.OrganNumber == int(parentelementpair[1]))
        if parentorgan == None :
          print("Did not find parent organ, gonna stop trying to add junction")
        else :
          try :
            parentsocket = parentorgan.Nodes[parentnode]
            parentdiamet = parentorgan.Functions["diameter"][parentnode]
            childsocket = points[0]
            points.insert(0,parentsocket + (childsocket-parentsocket)*parentdiamet)
            #params["diameter"].insert(0,parentdiamet)
            params["diameter"] = np.insert(params["diameter"],0,parentdiamet,axis=0)
            print("Successfully added junction")
          except :
            print("Something else went wrong when trying to add the junction")
      sr = []
      for e in element.findall('root') :
        # check if this is not just an empty <root/> element
        if e.get('ID') == None :
          continue
        elif e.get('ID') != element.get('ID') :
          elementlist.append([e,element.get('ID')])
          sr.append(int(e.get('ID')))
      organlist.append(Organ(np.array(points),[].copy(),params.copy(),int(element.get('ID')), sr.copy(),int(rn), parentnode))
    #endwhile
    return organlist
  #enddef
  def SetFilename(self,filename) :
    self.fname = filename
  #enddef
# end class ParseRSML

default_selection = ["diameter","length","area","count"]

# setup argparse for this script with arguments file, output, verbose, selection
parser = argparse.ArgumentParser(description='Convert RSML to VTP')
parser.add_argument('file', metavar='file', type=str, nargs=1,
                    help='the rsml file to quantify')
parser.add_argument('-v', '--verbose', action='store_true',
                    help='verbose output')
parser.add_argument('-o', '--output', metavar='output', type=str, nargs=1,
                    help='the output file')
parser.add_argument('-s', '--selection', metavar='selection', type=str, nargs=1,
                    help='the selection to quantify')

if __name__ == "__main__" :
  print("This is the RSML parser")
  args = parser.parse_args()
  if args.verbose :
    print("Verbose output activated")
  if args.selection :
    selected = selection.split(",")
    selected = [s for s in selected if s in default_selection]
    if len(selected) == 0 :
      print("No valid selection given, using default")
      selected = default_selection
    else :
      print("Using selection " + str(selected))
  else :
    selected = default_selection
    print("No selection given, using default")
  file = args.file[0]
  print("Parsing file " + str(file))
  reader = ParseRSML()
  reader.SetFilename(file)
  organs = reader.ParseFile()
  for organ in organs :
    print("="*80)
    print("Organ " + str(organ.OrganNumber))
    if "diameter" in selected :
      diameters = organ.Functions["diameter"]
      mean = np.mean(diameters)
      std = np.std(diameters)
      print("Mean diameter: " + str(mean))
      print("Std diameter: " + str(std))
    if "length" in selected :
      length = np.sum(np.linalg.norm(organ.Nodes[1:]-organ.Nodes[:-1],axis=1))
      # check for parent node
      if organ.ParentNode >= 0 :
        # add the distance to the parent node
        parent = next(o for o in organs if o.OrganNumber == organ.Predecessor)
        if parent != None :
          length += np.linalg.norm(organ.Nodes[0]-parent.Nodes[organ.ParentNode])
      print("Length: " + str(length))
    if "area" in selected :
      diameters = organ.Functions["diameter"]
      nodes = organ.Nodes
      area = 0
      for i in range(len(nodes)-1) :
        # calculate the area of the trapezoid
        area += (diameters[i]+diameters[i+1])/2*np.linalg.norm(nodes[i]-nodes[i+1])
      print("Area: " + str(area))
    if "count" in selected :
      print("Count: " + str(len(organ.Nodes)))
      print("Organ has " + str(len(organ.Suborgans)) + " suborgans")
      has_parent = organ.Predecessor >= 0
      print("Organ has parent: " + str(has_parent))
      if has_parent :
        print("Parent organ is " + str(organ.Predecessor))
        print("Parent node is " + str(organ.ParentNode))
    #endif
  #endfor
#

#endmain
      

#endif
